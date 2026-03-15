import asyncio
import json
import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Optional
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
import os

load_dotenv(override=True)

app = FastAPI(title="Gatekeeper", version="1.0.0")

# --- CONFIGURATION ---
ADGUARD_URL = os.getenv("ADGUARD_URL")
AUTH = (os.getenv("ADGUARD_USER_NAME"), os.getenv("ADGUARD_PASSWORD"))
VALID_PIN = os.getenv("ADGUARD_VALID_PIN")

with open("services_config.json") as _f:
    _BLOCKABLE_SERVICES: list = json.load(_f).get("services", [])

# ─── REQUEST MODELS ───────────────────────────────────────────────────────────

class ClientMutationRequest(BaseModel):
    """For operations that may create or modify a client record."""
    pin: str
    client_name: str  # Friendly name used when auto-creating the client (e.g., Son-Pi)


class TemporaryUnblockRequest(BaseModel):
    pin: str
    client_name: str
    duration_minutes: int


class IsolationRequest(BaseModel):
    """For internet isolation operations; client_id is supplied in the URL path."""
    pin: str


class PinVerifyRequest(BaseModel):
    """For validating the UI PIN gate before any control screens are shown."""
    pin: str

# ─── HELPERS ──────────────────────────────────────────────────────────────────

async def get_client(client_id: str) -> Optional[dict]:
    """Read-only lookup. Returns client data dict or None if not found."""
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.post(
            f"{ADGUARD_URL}/clients/search",
            json={"clients": [{"id": client_id}]},
        )
        resp.raise_for_status()
        results = resp.json()
    if results and results[0]:
        return list(results[0].values())[0]
    return None


async def get_or_prep_client(client_id: str, client_name: str) -> dict:
    """Return existing client data, or create a new persistent client and return it."""
    data = await get_client(client_id)
    if data:
        return data
    new_client = {
        "name": client_name,
        "ids": [client_id],
        "use_global_blocked_services": False,  # Essential for per-client service rules
        "filtering_enabled": True,
        "blocked_services": [],
    }
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.post(f"{ADGUARD_URL}/clients/add", json=new_client)
        resp.raise_for_status()
    return new_client


async def set_service_block_state(
    client_id: str, client_name: str, service: str, blocked: bool
) -> None:
    """Add or remove a single service from a client's blocked_services list."""
    client_data = await get_or_prep_client(client_id, client_name)
    services = list(client_data.get("blocked_services") or [])
    if blocked and service not in services:
        services.append(service)
    elif not blocked and service in services:
        services.remove(service)
    else:
        return  # Already in desired state; skip unnecessary write
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.post(
            f"{ADGUARD_URL}/clients/update",
            json={"name": client_name, "data": {**client_data, "blocked_services": services}},
        )
        resp.raise_for_status()


async def clear_all_blocked_services(client_id: str, client_name: str) -> list:
    """Clear all blocked services for a client. Returns the list that was cleared."""
    client_data = await get_or_prep_client(client_id, client_name)
    services = list(client_data.get("blocked_services") or [])
    if not services:
        return []
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.post(
            f"{ADGUARD_URL}/clients/update",
            json={"name": client_name, "data": {**client_data, "blocked_services": []}},
        )
        resp.raise_for_status()
    return services


async def list_blocked_services(client_id: str) -> list:
    """Return the blocked_services list for a client (empty list if client not found)."""
    data = await get_client(client_id)
    if data is None:
        return []
    return list(data.get("blocked_services") or [])


async def set_internet_isolation(client_id: str, isolated: bool) -> bool:
    """
    Add or remove client from AdGuard's disallowed_clients access list.
    Returns True if the list was modified, False if already in desired state (idempotent).
    """
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.get(f"{ADGUARD_URL}/access/list")
        resp.raise_for_status()
        access_list = resp.json()
        disallowed: list = list(access_list.get("disallowed_clients") or [])
        if isolated and client_id not in disallowed:
            disallowed.append(client_id)
        elif not isolated and client_id in disallowed:
            disallowed.remove(client_id)
        else:
            return False  # Already in desired state
        # /access/set is a full-replace: all three fields must be sent back
        # or AdGuard will silently clear allowed_clients and blocked_hosts
        set_resp = await http.post(
            f"{ADGUARD_URL}/access/set",
            json={
                "allowed_clients": access_list.get("allowed_clients") or [],
                "disallowed_clients": disallowed,
                "blocked_hosts": access_list.get("blocked_hosts") or [],
            },
        )
        set_resp.raise_for_status()
    return True


async def get_internet_isolation_state(client_id: str) -> bool:
    """Return True if the client is currently in AdGuard's disallowed_clients list."""
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.get(f"{ADGUARD_URL}/access/list")
        resp.raise_for_status()
        return client_id in resp.json().get("disallowed_clients", [])


async def temporary_unblock_then_reblock(
    client_id: str, client_name: str, service: str, duration_minutes: int
) -> None:
    """Unblock a service, wait duration_minutes, then re-block it."""
    await set_service_block_state(client_id, client_name, service, blocked=False)
    await asyncio.sleep(duration_minutes * 60)
    await set_service_block_state(client_id, client_name, service, blocked=True)


def _require_pin(pin: str) -> None:
    if pin != VALID_PIN:
        raise HTTPException(status_code=401, detail="Invalid PIN")


def _upstream_error(exc: httpx.HTTPStatusError) -> HTTPException:
    return HTTPException(status_code=502, detail=f"AdGuard upstream error: {exc.response.status_code}")


async def list_persistent_clients_raw() -> list:
    """Fetch all persistent clients from AdGuard. Read-only, no side effects."""
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.get(f"{ADGUARD_URL}/clients")
        resp.raise_for_status()
        return resp.json().get("clients", [])

# ─── SYSTEM ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/api/v1/auth/verify-pin")
async def verify_pin(req: PinVerifyRequest):
    """Validate the admin PIN used by the UI gate."""
    _require_pin(req.pin)
    return {"valid": True}


@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("index.html") as f:
        return f.read()


# ─── CLIENT LIST ENDPOINTS ────────────────────────────────────────────────────

@app.get("/api/v1/clients")
async def list_clients():
    """List all persistent clients for UI dropdown. Returns client_id and client_name only."""
    try:
        raw_clients = await list_persistent_clients_raw()
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)

    seen_ids = set()
    clients = []
    for c in raw_clients:
        name = c.get("name")
        ids = c.get("ids") or []
        if not ids:
            continue  # skip clients without usable identifier
        client_id = ids[0]
        if client_id in seen_ids:
            continue  # deduplicate
        seen_ids.add(client_id)
        clients.append({"client_id": client_id, "client_name": name or client_id})

    # deterministic ordering: by name (case-insensitive), then id
    clients.sort(key=lambda x: (x["client_name"].lower(), x["client_id"]))
    return {"clients": clients}


# ─── BLOCKABLE SERVICES CATALOGUE ───────────────────────────────────────────

@app.get("/api/v1/services/blockable")
async def list_blockable_services(category: Optional[str] = None):
    """Return the curated catalogue of blockable services for the UI selector.
    Pass ?category=social|messaging|streaming|gaming to filter by category.
    """
    services = _BLOCKABLE_SERVICES
    if category:
        services = [s for s in services if s.get("category") == category.lower()]
    categories = sorted({s["category"] for s in _BLOCKABLE_SERVICES})
    return {"categories": categories, "services": services}


# ─── SERVICE BLOCK ENDPOINTS ──────────────────────────────────────────────────

@app.get("/api/v1/clients/{client_id}/services/blocked")
async def get_blocked_services(client_id: str):
    """List all currently blocked services for a client."""
    try:
        services = await list_blocked_services(client_id)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "blocked_services": services}


@app.get("/api/v1/clients/{client_id}/services/blocked/{service_id}")
async def get_service_block_status(client_id: str, service_id: str):
    """Return whether a specific service is currently blocked for a client."""
    try:
        services = await list_blocked_services(client_id)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "service_id": service_id, "blocked": service_id in services}


@app.put("/api/v1/clients/{client_id}/services/blocked/{service_id}")
async def block_service(client_id: str, service_id: str, req: ClientMutationRequest):
    """Permanently block a specific service for a client."""
    _require_pin(req.pin)
    try:
        await set_service_block_state(client_id, req.client_name, service_id, blocked=True)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "service_id": service_id, "blocked": True}


@app.delete("/api/v1/clients/{client_id}/services/blocked/{service_id}")
async def unblock_service(client_id: str, service_id: str, req: ClientMutationRequest):
    """Permanently unblock a specific service for a client."""
    _require_pin(req.pin)
    try:
        await set_service_block_state(client_id, req.client_name, service_id, blocked=False)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "service_id": service_id, "blocked": False}


@app.delete("/api/v1/clients/{client_id}/services/blocked")
async def unblock_all_services(client_id: str, req: ClientMutationRequest):
    """Permanently unblock all services for a client."""
    _require_pin(req.pin)
    try:
        cleared = await clear_all_blocked_services(client_id, req.client_name)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "unblocked_services": cleared}


@app.post("/api/v1/clients/{client_id}/services/blocked/{service_id}/temporary-unblock")
async def temporary_unblock_service(
    client_id: str, service_id: str, req: TemporaryUnblockRequest, bg: BackgroundTasks
):
    """Unblock a service for a set number of minutes, then automatically re-block it."""
    _require_pin(req.pin)
    bg.add_task(
        temporary_unblock_then_reblock,
        client_id, req.client_name, service_id, req.duration_minutes,
    )
    return {
        "client_id": client_id,
        "service_id": service_id,
        "duration_minutes": req.duration_minutes,
        "status": "temporary_unblock_scheduled",
    }


# ─── INTERNET ISOLATION ENDPOINTS ─────────────────────────────────────────────

@app.get("/api/v1/clients/{client_id}/internet/isolation")
async def get_isolation_status(client_id: str):
    """Return whether a client is currently internet-isolated."""
    try:
        isolated = await get_internet_isolation_state(client_id)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "isolated": isolated}


@app.put("/api/v1/clients/{client_id}/internet/isolation")
async def isolate_client(client_id: str, req: IsolationRequest):
    """Block all internet traffic for a client (add to AdGuard disallowed list)."""
    _require_pin(req.pin)
    try:
        modified = await set_internet_isolation(client_id, isolated=True)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "isolated": True, "modified": modified}


@app.delete("/api/v1/clients/{client_id}/internet/isolation")
async def restore_client_internet(client_id: str, req: IsolationRequest):
    """Restore full internet access for a client (remove from AdGuard disallowed list)."""
    _require_pin(req.pin)
    try:
        modified = await set_internet_isolation(client_id, isolated=False)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "isolated": False, "modified": modified}