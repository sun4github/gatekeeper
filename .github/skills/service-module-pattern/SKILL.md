---
name: service-module-pattern
description: "Create or refactor Python service modules for gatekeeper using async-first patterns, idempotent side effects, and safe state tracking. Use for new app support services, scheduler-style jobs, and AdGuard-style integration services under app/services only."
argument-hint: "Describe the service purpose, external dependencies, async flows, state keys, and required public functions."
user-invocable: true
---

# Service Module Pattern

## What This Skill Produces
- A focused Python service module placed only in `app/services/`.
- Async-first functions with clear public helpers and private internals.
- Idempotent external state mutation logic.
- Predictable cancellation and cleanup for background tasks when scheduling is needed.
- Stable return types and concise docstrings that match API/service callers.

## Hard Requirement
- Create new service files only in `app/services/`.
- Do not place service business logic in `app/api/`, `app/core/`, or repository root files.

## When To Use
- Adding a new integration service (HTTP provider, DNS provider, policy provider).
- Adding scheduler-backed service workflows (temporary actions, delayed rollback/reblock).
- Refactoring mixed route-plus-logic code into a reusable service layer.
- Building reusable helper functions consumed by API routers.

## Inputs To Collect First
1. Service name and responsibility boundary.
2. Public functions expected by callers.
3. External dependencies (HTTP endpoints, DB helpers, config values).
4. Whether behavior is read-only, mutating, or both.
5. Whether delayed jobs or task cancellation semantics are required.
6. Return shape contract (bool, dict, list, or None).

## Procedure
1. Place module in `app/services/` and name it by domain.
Example names: `adguard.py`, `scheduler.py`, `notifications.py`.

2. Define clear function boundaries.
- Public functions: caller-oriented names, explicit parameters, deterministic outputs.
- Private helpers: underscore-prefixed for internals like key builders or cancellation wrappers.

3. Use async and idempotent mutation logic.
- Use `async def` for I/O and provider calls.
- For list/set-like state updates, no-op when already in desired state.
- Skip unnecessary writes when no state change is needed.

4. Handle external failures safely.
- Call `raise_for_status()` for HTTP writes/reads where relevant.
- Keep error handling narrow and explicit.
- Use best-effort cleanup for non-critical close/log actions.

5. Implement scheduler/task safety only when needed.
- Track tasks by stable keys (for example `(client_id, service)`).
- Protect shared task maps with `asyncio.Lock()`.
- On replacement, cancel prior task and await cancellation safely.
- In `finally`, remove task metadata only if current task still owns the key.

6. Keep metadata and debug snapshots deterministic.
- Store start/end epochs for timed jobs.
- Expose debug snapshot helpers with stable keys and computed remaining time.
- Remove stale done-tasks before building snapshots.

7. Keep service layer independent from route concerns.
- No FastAPI request/response classes in service modules.
- Service modules may call `app/core/*`, `app/services/*`, and DB helper functions as needed.

8. Validate quickly after implementation.
- Confirm file path is under `app/services/`.
- Confirm public functions have docstrings and deterministic return types.
- Confirm idempotent behavior for repeated calls.
- Confirm task cancellation paths clean metadata without leaking tasks.

## Decision Points
- Need immediate value vs reusable helper:
If logic is one-off and route-specific, keep minimal helper in service anyway if it touches external systems or state.

- Single function vs module split:
If module exceeds one responsibility (for example provider API + scheduling + reporting), split into separate service modules.

- Return bool vs payload:
Use bool for idempotent "changed vs unchanged" operations. Use structured dict/list for caller-facing data.

## Completion Checklist
1. Service code exists in `app/services/<name>.py`.
2. Public functions are async where I/O is present.
3. Side effects are idempotent and skip no-op writes.
4. Cancellation-safe task handling is implemented for timed jobs.
5. Shared mutable task state is lock-protected.
6. Non-critical cleanup paths use best-effort error suppression only where justified.
7. Return shapes are explicit and stable.
8. Docstrings describe behavior and side effects.

## Output Skeleton
```python
import asyncio
from contextlib import suppress

_STATE: dict[str, asyncio.Task] = {}
_STATE_LOCK = asyncio.Lock()


async def perform_action(resource_id: str) -> bool:
    """Apply a state change and return whether a change occurred."""
    # Fetch current state, no-op if already desired, then persist.
    return True


async def schedule_action(resource_id: str, seconds: int) -> bool:
    """Schedule delayed action and replace existing timer if present."""
    key = resource_id
    task = asyncio.create_task(_job(resource_id, seconds), name=f"job:{resource_id}")
    async with _STATE_LOCK:
        old = _STATE.pop(key, None)
        _STATE[key] = task
    if old is not None:
        old.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await old
    return old is not None


async def _job(resource_id: str, seconds: int) -> None:
    try:
        await asyncio.sleep(seconds)
        await perform_action(resource_id)
    finally:
        async with _STATE_LOCK:
            if _STATE.get(resource_id) is asyncio.current_task():
                _STATE.pop(resource_id, None)
```

## In-Repo References
- Integration/idempotent mutation pattern: [adguard service](../../../app/services/adguard.py)
- Task tracking/cancellation pattern: [scheduler service](../../../app/services/scheduler.py)
