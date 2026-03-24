---
name: fastapi-router-endpoints
description: "Create FastAPI routers and REST endpoints with versioned paths, request validation, auth guards, upstream error mapping, and deterministic response payloads. Use for new API modules, endpoint migrations, and consistent router design in Python projects."
argument-hint: "Describe the resource, routes, auth needs, request schemas, and side effects."
user-invocable: true
---

# FastAPI Router Endpoints

## What This Skill Produces
- A production-ready FastAPI router module with clear endpoint groups.
- Consistent route paths (for example, /api/v1/... patterns).
- Request body validation via Pydantic schemas.
- Reliable error handling for auth, upstream HTTP failures, and local persistence failures.
- Stable JSON response shapes suitable for UI and automation.

## When To Use
- Adding a new API resource with multiple CRUD-like operations.
- Refactoring ad-hoc route functions into a cohesive router.
- Building APIs that call upstream services and must map failures to clear HTTP errors.
- Creating admin-protected mutation endpoints.

## Inputs To Collect First
1. Resource name and domain actions (read, create, update, delete, custom actions).
2. URL structure and versioning policy (for example, /api/v1/<resource>).
3. Auth requirements per route (public read, protected mutation, role-based, etc.).
4. Request and response schema contracts.
5. Side effects (database writes, scheduler jobs, upstream API calls).
6. Deterministic ordering rules for list responses.

## Procedure
1. Establish router module boundaries.
Create a dedicated router file under an API package and initialize APIRouter once.

2. Define helper guards and translators before endpoint functions.
- Add auth guard helpers (for example, _require_pin).
- Add upstream exception translators (for example, map httpx.HTTPStatusError to HTTPException 502).
- Keep helpers small and reuse them across endpoints.

3. Implement endpoints by capability groups.
- Group routes by responsibility with section headers: system, resource listing, mutations, analytics, or debug.
- Name route handlers with action-oriented function names.
- Add concise docstrings that describe behavior and intent.

4. Apply branching rules for each endpoint.
- If endpoint mutates state, enforce auth guard before side effects.
- If endpoint calls upstream HTTP services, wrap those calls in try/except and convert failures to an API-safe status/detail.
- If endpoint performs local persistence work after an upstream success, isolate that in a second try/except so caller receives explicit local failure diagnostics.
- If endpoint returns list data, deduplicate and sort deterministically (case-insensitive primary key, stable fallback key).
- If reverse proxies can break dotted filenames or special routes, provide alias paths.

5. Keep response payloads explicit and stable.
- Always include key identifiers in responses (for example, resource id, operation target).
- Return operation state booleans and metadata fields needed by callers.
- Prefer structured dictionaries over implicit tuples or positional arrays.

6. Register router in app composition.
- Include the router from app startup assembly (for example, app.include_router(...)).
- Keep route registration centralized so API surface is easy to audit.

7. Validate behavior quickly.
- Verify every protected endpoint rejects invalid credentials.
- Verify upstream failures return mapped HTTP errors.
- Verify list endpoints return deterministic ordering.
- Verify route aliases resolve the same payload as canonical endpoints.

## Decision Points
- Auth model:
Use lightweight helper guard for shared credential checks. Use dependency injection if auth policy is complex or per-role.

- Error mapping:
Use 502 for upstream provider failures and 500 for local persistence/scheduler failures unless product requirements demand finer mapping.

- Route naming:
Use noun-based paths for resources and verb-like suffixes only for non-CRUD actions (for example, /temporary-unblock).

## Completion Checklist
1. Router file has one APIRouter instance and grouped endpoint sections.
2. Request bodies are typed with Pydantic schemas.
3. Protected mutations call auth guard before mutation.
4. Upstream exceptions are translated to API-safe HTTPException values.
5. Local side effect failures are handled separately with explicit messages.
6. List outputs are deduplicated and sorted deterministically.
7. Router is registered in application bootstrap.
8. Every endpoint returns a stable JSON object with identifiers and status fields.

## Output Template (Starter)
```python
import httpx
from fastapi import APIRouter, HTTPException

from app.schemas.example import ExampleCreateRequest
from app.services.example_service import create_example, list_examples

router = APIRouter()


def _require_pin(pin: str, valid_pin: str) -> None:
    if pin != valid_pin:
        raise HTTPException(status_code=401, detail="Invalid PIN")


def _upstream_error(exc: httpx.HTTPStatusError) -> HTTPException:
    return HTTPException(status_code=502, detail=f"Upstream error: {exc.response.status_code}")


@router.get("/api/v1/examples")
async def get_examples():
    try:
        items = await list_examples()
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    items.sort(key=lambda x: (x["name"].lower(), x["id"]))
    return {"examples": items}


@router.post("/api/v1/examples")
async def post_example(req: ExampleCreateRequest):
    _require_pin(req.pin, "<inject-valid-pin>")
    try:
        created = await create_example(req)
    except httpx.HTTPStatusError as exc:
        raise _upstream_error(exc)
    return {"id": created["id"], "created": True}
```

## In-Repo References
- Router patterns: [v1 example](../../../app/api/v1.py)
- UI file-serving routes and aliasing: [ui example](../../../app/api/ui.py)
- Router composition: [app include router example](../../../app/main.py)
