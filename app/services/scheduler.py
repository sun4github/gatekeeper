import asyncio
import time
from contextlib import suppress

from app.core.database import db_close_viewing_event
from app.services.adguard import set_service_block_state

_TEMP_UNBLOCK_TASKS: dict[tuple[str, str], asyncio.Task] = {}
_TEMP_UNBLOCK_META: dict[tuple[str, str], dict] = {}
_TEMP_UNBLOCK_TASKS_LOCK = asyncio.Lock()


def _temp_unblock_key(client_id: str, service: str) -> tuple[str, str]:
    return (client_id, service)


async def _cancel_tracked_task(task: asyncio.Task) -> None:
    task.cancel()
    # Ignore cancellation-time errors from old tasks; cleanup must stay best-effort.
    with suppress(asyncio.CancelledError, Exception):
        await task


async def temporary_unblock_then_reblock(
    client_id: str, client_name: str, service: str, duration_minutes: int
) -> None:
    """Unblock a service, wait duration_minutes, then re-block it."""
    await set_service_block_state(client_id, client_name, service, blocked=False)
    await asyncio.sleep(duration_minutes * 60)
    await set_service_block_state(client_id, client_name, service, blocked=True)


async def _tracked_temporary_unblock_job(
    client_id: str, client_name: str, service: str, duration_minutes: int, user_name: str = ""
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
    # Natural completion only (CancelledError was re-raised above): reblock happened, close event.
    if user_name:
        with suppress(Exception):
            await db_close_viewing_event(user_name, client_id, service)


async def cancel_temporary_unblock_job(client_id: str, service: str) -> bool:
    """Cancel a pending temporary-unblock timer for a specific client/service."""
    key = _temp_unblock_key(client_id, service)
    async with _TEMP_UNBLOCK_TASKS_LOCK:
        task = _TEMP_UNBLOCK_TASKS.pop(key, None)
        meta = _TEMP_UNBLOCK_META.pop(key, None)
    if not task:
        return False
    await _cancel_tracked_task(task)
    if meta and meta.get("user_name"):
        with suppress(Exception):
            await db_close_viewing_event(meta["user_name"], client_id, service)
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


async def schedule_temporary_unblock(
    client_id: str, client_name: str, service: str, duration_minutes: int, user_name: str = ""
) -> bool:
    """Schedule temporary unblock and replace any existing timer for same client/service."""
    key = _temp_unblock_key(client_id, service)
    task = asyncio.create_task(
        _tracked_temporary_unblock_job(client_id, client_name, service, duration_minutes, user_name),
        name=f"temp-unblock:{client_id}:{service}",
    )
    start_epoch = time.time()
    async with _TEMP_UNBLOCK_TASKS_LOCK:
        old_task = _TEMP_UNBLOCK_TASKS.pop(key, None)
        old_meta = _TEMP_UNBLOCK_META.pop(key, None)
        _TEMP_UNBLOCK_TASKS[key] = task
        _TEMP_UNBLOCK_META[key] = {
            "user_name": user_name,
            "client_name": client_name,
            "duration_minutes": duration_minutes,
            "started_at_epoch": start_epoch,
            "ends_at_epoch": start_epoch + (duration_minutes * 60),
        }
    if old_task is not None:
        await _cancel_tracked_task(old_task)
        if old_meta and old_meta.get("user_name"):
            with suppress(Exception):
                await db_close_viewing_event(old_meta["user_name"], client_id, service)
    return old_task is not None


async def get_jobs_debug_snapshot() -> list:
    """Return a snapshot of active job metadata for debugging."""
    now_epoch = time.time()
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
    return jobs
