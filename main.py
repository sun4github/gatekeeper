import asyncio
import json
from contextlib import suppress
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import time
from typing import Optional
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from dotenv import load_dotenv
import os

load_dotenv(override=True)

app = FastAPI(title="Gatekeeper", version="1.0.0")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

# --- CONFIGURATION ---
ADGUARD_URL = os.getenv("ADGUARD_URL")
AUTH = (os.getenv("ADGUARD_USER_NAME"), os.getenv("ADGUARD_PASSWORD"))
VALID_PIN = os.getenv("ADGUARD_VALID_PIN")

with open("services_config.json") as _f:
    _BLOCKABLE_SERVICES: list = json.load(_f).get("services", [])

_TEMP_UNBLOCK_TASKS: dict[tuple[str, str], asyncio.Task] = {}
_TEMP_UNBLOCK_META: dict[tuple[str, str], dict] = {}
_TEMP_UNBLOCK_TASKS_LOCK = asyncio.Lock()

# ─── REQUEST MODELS ───────────────────────────────────────────────────────────

class ClientMutationRequest(BaseModel):
    """For operations that may create or modify a client record."""
    pin: str
    client_name: str  # Friendly name used when auto-creating the client (e.g., Son-Pi)


class TemporaryUnblockRequest(BaseModel):
    pin: str
    client_name: str
    duration_minutes: int = Field(..., ge=1, le=120)


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


def _temp_unblock_key(client_id: str, service: str) -> tuple[str, str]:
    return (client_id, service)


async def _cancel_tracked_task(task: asyncio.Task) -> None:
    task.cancel()
    # Ignore cancellation-time errors from old tasks; cleanup must stay best-effort.
    with suppress(asyncio.CancelledError, Exception):
        await task


async def cancel_temporary_unblock_job(client_id: str, service: str) -> bool:
    """Cancel a pending temporary-unblock timer for a specific client/service."""
    key = _temp_unblock_key(client_id, service)
    async with _TEMP_UNBLOCK_TASKS_LOCK:
        task = _TEMP_UNBLOCK_TASKS.pop(key, None)
        _TEMP_UNBLOCK_META.pop(key, None)
    if not task:
        return False
    await _cancel_tracked_task(task)
    return True


async def cancel_temporary_unblock_jobs_for_client(client_id: str) -> int:
    """Cancel all pending temporary-unblock timers for a client."""
    async with _TEMP_UNBLOCK_TASKS_LOCK:
        keys = [k for k in _TEMP_UNBLOCK_TASKS if k[0] == client_id]
        tasks = [_TEMP_UNBLOCK_TASKS.pop(k) for k in keys]
        for key in keys:
            _TEMP_UNBLOCK_META.pop(key, None)
    for task in tasks:
        await _cancel_tracked_task(task)
    return len(tasks)


async def cancel_all_temporary_unblock_jobs() -> int:
    """Cancel all pending temporary-unblock timers across all clients."""
    async with _TEMP_UNBLOCK_TASKS_LOCK:
        tasks = list(_TEMP_UNBLOCK_TASKS.values())
        _TEMP_UNBLOCK_TASKS.clear()
        _TEMP_UNBLOCK_META.clear()
    for task in tasks:
        await _cancel_tracked_task(task)
    return len(tasks)


async def _tracked_temporary_unblock_job(
    client_id: str, client_name: str, service: str, duration_minutes: int
) -> None:
    key = _temp_unblock_key(client_id, service)
    try:
        await temporary_unblock_then_reblock(client_id, client_name, service, duration_minutes)
    except asyncio.CancelledError:
        raise
    finally:
        async with _TEMP_UNBLOCK_TASKS_LOCK:
            current = _TEMP_UNBLOCK_TASKS.get(key)
            if current is asyncio.current_task():
                _TEMP_UNBLOCK_TASKS.pop(key, None)
                _TEMP_UNBLOCK_META.pop(key, None)


async def schedule_temporary_unblock(
    client_id: str, client_name: str, service: str, duration_minutes: int
) -> bool:
    """Schedule temporary unblock and replace any existing timer for same client/service."""
    replaced_existing = await cancel_temporary_unblock_job(client_id, service)
    key = _temp_unblock_key(client_id, service)
    task = asyncio.create_task(
        _tracked_temporary_unblock_job(client_id, client_name, service, duration_minutes),
        name=f"temp-unblock:{client_id}:{service}",
    )
    async with _TEMP_UNBLOCK_TASKS_LOCK:
        _TEMP_UNBLOCK_TASKS[key] = task
        start_epoch = time.time()
        _TEMP_UNBLOCK_META[key] = {
            "client_name": client_name,
            "duration_minutes": duration_minutes,
            "started_at_epoch": start_epoch,
            "ends_at_epoch": start_epoch + (duration_minutes * 60),
        }
    return replaced_existing


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


@app.on_event("shutdown")
async def on_shutdown_cleanup_jobs():
    """Ensure no sleeping temporary-unblock tasks block shutdown/reload."""
    await cancel_all_temporary_unblock_jobs()


@app.post("/api/v1/auth/verify-pin")
async def verify_pin(req: PinVerifyRequest):
    """Validate the admin PIN used by the UI gate."""
    _require_pin(req.pin)
    return {"valid": True}


@app.post("/api/v1/debug/temporary-jobs")
async def debug_temporary_jobs(req: PinVerifyRequest):
    """List active temporary-unblock timers for operational debugging."""
    _require_pin(req.pin)

    now_epoch = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    async with _TEMP_UNBLOCK_TASKS_LOCK:
        stale_keys = [k for k, task in _TEMP_UNBLOCK_TASKS.items() if task.done()]
        for key in stale_keys:
            _TEMP_UNBLOCK_TASKS.pop(key, None)
            _TEMP_UNBLOCK_META.pop(key, None)

        jobs = []
        for (client_id, service), task in _TEMP_UNBLOCK_TASKS.items():
            meta = _TEMP_UNBLOCK_META.get((client_id, service), {})
            ends_at_epoch = meta.get("ends_at_epoch")
            seconds_remaining = None
            if isinstance(ends_at_epoch, (int, float)):
                seconds_remaining = max(0, int(ends_at_epoch - now_epoch))
            jobs.append(
                {
                    "client_id": client_id,
                    "client_name": meta.get("client_name") or client_id,
                    "service_id": service,
                    "duration_minutes": meta.get("duration_minutes"),
                    "started_at_epoch": meta.get("started_at_epoch"),
                    "ends_at_epoch": ends_at_epoch,
                    "seconds_remaining": seconds_remaining,
                    "task_done": task.done(),
                    "task_cancelled": task.cancelled(),
                }
            )

    jobs.sort(key=lambda x: (x["client_name"].lower(), x["service_id"]))
    return {
        "now_utc": now_iso,
        "active_jobs_count": len(jobs),
        "jobs": jobs,
    }


@app.get("/api/v1/ui/manifest.webmanifest")
async def ui_manifest():
    """Web app manifest served via API path for reverse-proxy compatibility."""
    return JSONResponse(
        content={
            "name": "Gatekeeper",
            "short_name": "Gatekeeper",
            "description": "Parental control console for internet and service blocking.",
            "id": "/gatekeeper/",
            "start_url": "/gatekeeper/",
            "scope": "/gatekeeper/",
            "display": "standalone",
            "background_color": "#131926",
            "theme_color": "#224f6e",
            "icons": [
                {
                    "src": "/gatekeeper/api/v1/ui/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/gatekeeper/api/v1/ui/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/gatekeeper/api/v1/ui/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/api/v1/ui/manifest")
async def ui_manifest_alias():
    """Alias path for proxies that do not forward dotted filenames reliably."""
    return await ui_manifest()


@app.get("/api/v1/ui/icon-192.png")
async def ui_icon_192_png():
    return FileResponse("assets/icon-192.png", media_type="image/png")


@app.get("/api/v1/ui/icon-192")
async def ui_icon_192_png_alias():
    return await ui_icon_192_png()


@app.get("/api/v1/ui/icon-512.png")
async def ui_icon_512_png():
    return FileResponse("assets/icon-512.png", media_type="image/png")


@app.get("/api/v1/ui/icon-512")
async def ui_icon_512_png_alias():
    return await ui_icon_512_png()


@app.get("/api/v1/ui/icon-maskable-512.png")
async def ui_icon_maskable_512_png():
    return FileResponse("assets/icon-maskable-512.png", media_type="image/png")


@app.get("/api/v1/ui/icon-maskable-512")
async def ui_icon_maskable_512_png_alias():
    return await ui_icon_maskable_512_png()


@app.get("/api/v1/ui/icon.svg")
async def ui_icon_svg():
    return FileResponse("assets/icon.svg", media_type="image/svg+xml")


@app.get("/api/v1/ui/icon")
async def ui_icon_svg_alias():
    return await ui_icon_svg()


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
        await cancel_temporary_unblock_job(client_id, service_id)
        await set_service_block_state(client_id, req.client_name, service_id, blocked=True)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "service_id": service_id, "blocked": True}


@app.delete("/api/v1/clients/{client_id}/services/blocked/{service_id}")
async def unblock_service(client_id: str, service_id: str, req: ClientMutationRequest):
    """Permanently unblock a specific service for a client."""
    _require_pin(req.pin)
    try:
        await cancel_temporary_unblock_job(client_id, service_id)
        await set_service_block_state(client_id, req.client_name, service_id, blocked=False)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "service_id": service_id, "blocked": False}


@app.delete("/api/v1/clients/{client_id}/services/blocked")
async def unblock_all_services(client_id: str, req: ClientMutationRequest):
    """Permanently unblock all services for a client."""
    _require_pin(req.pin)
    try:
        cancelled_jobs = await cancel_temporary_unblock_jobs_for_client(client_id)
        cleared = await clear_all_blocked_services(client_id, req.client_name)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {
        "client_id": client_id,
        "unblocked_services": cleared,
        "cancelled_temporary_jobs": cancelled_jobs,
    }


@app.post("/api/v1/clients/{client_id}/services/blocked/{service_id}/temporary-unblock")
async def temporary_unblock_service(
    client_id: str, service_id: str, req: TemporaryUnblockRequest
):
    """Unblock a service for a set number of minutes, then automatically re-block it."""
    _require_pin(req.pin)
    replaced_existing = await schedule_temporary_unblock(
        client_id, req.client_name, service_id, req.duration_minutes
    )
    return {
        "client_id": client_id,
        "service_id": service_id,
        "duration_minutes": req.duration_minutes,
        "replaced_existing_schedule": replaced_existing,
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