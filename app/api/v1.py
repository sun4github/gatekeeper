from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException

from app.core.config import BLOCKABLE_SERVICES, VALID_PIN, load_users
from app.core.database import (
    db_append_viewing_event,
    db_close_viewing_event,
    db_get_viewing_time_today,
)
from app.schemas.gatekeeper import (
    ClientMutationRequest,
    IsolationRequest,
    PinVerifyRequest,
    TemporaryUnblockRequest,
)
from app.services.adguard import (
    clear_all_blocked_services,
    get_internet_isolation_state,
    list_blocked_services,
    list_persistent_clients_raw,
    set_internet_isolation,
    set_service_block_state,
)
from app.services.scheduler import (
    cancel_temporary_unblock_job,
    cancel_temporary_unblock_jobs_for_client,
    get_jobs_debug_snapshot,
    schedule_temporary_unblock,
)

router = APIRouter()


def _require_pin(pin: str) -> None:
    if pin != VALID_PIN:
        raise HTTPException(status_code=401, detail="Invalid PIN")


def _upstream_error(exc: httpx.HTTPStatusError) -> HTTPException:
    return HTTPException(status_code=502, detail=f"AdGuard upstream error: {exc.response.status_code}")


# ─── SYSTEM ENDPOINTS ─────────────────────────────────────────────────────────

@router.get("/health")
async def health_check():
    return {"status": "ok"}


@router.post("/api/v1/auth/verify-pin")
async def verify_pin(req: PinVerifyRequest):
    """Validate the admin PIN used by the UI gate."""
    _require_pin(req.pin)
    return {"valid": True}


@router.post("/api/v1/debug/temporary-jobs")
async def debug_temporary_jobs(req: PinVerifyRequest):
    """List active temporary-unblock timers for operational debugging."""
    _require_pin(req.pin)
    now_iso = datetime.now(timezone.utc).isoformat()
    jobs = await get_jobs_debug_snapshot()
    jobs.sort(key=lambda x: (x["client_name"].lower(), x["service_id"]))
    return {
        "now_utc": now_iso,
        "active_jobs_count": len(jobs),
        "jobs": jobs,
    }


# ─── CLIENT LIST ENDPOINTS ────────────────────────────────────────────────────

@router.get("/api/v1/clients")
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


@router.get("/api/v1/users")
async def list_users():
    """List all users from users.json."""
    users = load_users()
    return {"users": users}


# ─── BLOCKABLE SERVICES CATALOGUE ───────────────────────────────────────────

@router.get("/api/v1/services/blockable")
async def list_blockable_services(category: Optional[str] = None):
    """Return the curated catalogue of blockable services for the UI selector.
    Pass ?category=social|messaging|streaming|gaming to filter by category.
    """
    services = BLOCKABLE_SERVICES
    if category:
        services = [s for s in services if s.get("category") == category.lower()]
    categories = sorted({s["category"] for s in BLOCKABLE_SERVICES})
    return {"categories": categories, "services": services}


# ─── SERVICE BLOCK ENDPOINTS ──────────────────────────────────────────────────

@router.get("/api/v1/clients/{client_id}/services/blocked")
async def get_blocked_services(client_id: str):
    """List all currently blocked services for a client."""
    try:
        services = await list_blocked_services(client_id)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "blocked_services": services}


@router.get("/api/v1/clients/{client_id}/services/blocked/{service_id}")
async def get_service_block_status(client_id: str, service_id: str):
    """Return whether a specific service is currently blocked for a client."""
    try:
        services = await list_blocked_services(client_id)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "service_id": service_id, "blocked": service_id in services}


@router.put("/api/v1/clients/{client_id}/services/blocked/{service_id}")
async def block_service(client_id: str, service_id: str, req: ClientMutationRequest):
    """Permanently block a specific service for a client."""
    _require_pin(req.pin)
    try:
        await cancel_temporary_unblock_job(client_id, service_id)
        await set_service_block_state(client_id, req.client_name, service_id, blocked=True)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    try:
        await db_close_viewing_event(req.user_name, client_id, service_id)
    except Exception:
        raise HTTPException(status_code=500, detail="Database error closing unblock event")
    return {"client_id": client_id, "service_id": service_id, "blocked": True}


@router.delete("/api/v1/clients/{client_id}/services/blocked/{service_id}")
async def unblock_service(client_id: str, service_id: str, req: ClientMutationRequest):
    """Permanently unblock a specific service for a client."""
    _require_pin(req.pin)
    try:
        await cancel_temporary_unblock_job(client_id, service_id)
        await set_service_block_state(client_id, req.client_name, service_id, blocked=False)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    try:
        await db_append_viewing_event(req.user_name, client_id, req.client_name, service_id, None)
    except Exception:
        raise HTTPException(status_code=500, detail="Database error recording unblock event")
    return {"client_id": client_id, "service_id": service_id, "blocked": False}


@router.delete("/api/v1/clients/{client_id}/services/blocked")
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


@router.post("/api/v1/clients/{client_id}/services/blocked/{service_id}/temporary-unblock")
async def temporary_unblock_service(
    client_id: str, service_id: str, req: TemporaryUnblockRequest
):
    """Unblock a service for a set number of minutes, then automatically re-block it."""
    _require_pin(req.pin)
    replaced_existing = await schedule_temporary_unblock(
        client_id, req.client_name, service_id, req.duration_minutes, req.user_name
    )
    try:
        await db_append_viewing_event(
            req.user_name, client_id, req.client_name, service_id, req.duration_minutes
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Database error recording unblock event")
    return {
        "client_id": client_id,
        "service_id": service_id,
        "duration_minutes": req.duration_minutes,
        "replaced_existing_schedule": replaced_existing,
        "status": "temporary_unblock_scheduled",
    }


# ─── VIEWING TIME ANALYTICS ────────────────────────────────────────────────────

@router.get("/api/v1/users/{user_name}/viewing-time")
async def get_user_viewing_time(user_name: str, device_id: Optional[str] = None):
    """Return today's per-service viewing seconds for a user, optionally filtered by device."""
    totals = await db_get_viewing_time_today(user_name, device_id)
    return {
        "user_name": user_name,
        "device_id": device_id,
        "services": totals,
    }


# ─── INTERNET ISOLATION ENDPOINTS ─────────────────────────────────────────────

@router.get("/api/v1/clients/{client_id}/internet/isolation")
async def get_isolation_status(client_id: str):
    """Return whether a client is currently internet-isolated."""
    try:
        isolated = await get_internet_isolation_state(client_id)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "isolated": isolated}


@router.put("/api/v1/clients/{client_id}/internet/isolation")
async def isolate_client(client_id: str, req: IsolationRequest):
    """Block all internet traffic for a client (add to AdGuard disallowed list)."""
    _require_pin(req.pin)
    try:
        modified = await set_internet_isolation(client_id, isolated=True)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "isolated": True, "modified": modified}


@router.delete("/api/v1/clients/{client_id}/internet/isolation")
async def restore_client_internet(client_id: str, req: IsolationRequest):
    """Restore full internet access for a client (remove from AdGuard disallowed list)."""
    _require_pin(req.pin)
    try:
        modified = await set_internet_isolation(client_id, isolated=False)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"client_id": client_id, "isolated": False, "modified": modified}
