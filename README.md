# Gatekeeper

A FastAPI-based parental control console that manages per-device internet access and
service blocking through an AdGuard Home backend.  A single-page web UI is served
alongside the API and works as an installable PWA.

---

## Key Features

- PIN-protected admin operations
- Per-client service blocking and unblocking (permanent or timed)
- Temporary unblock with automatic re-block after a configurable number of minutes
- Full internet isolation per client (AdGuard disallowed-clients list)
- Curated blockable-services catalogue with categories (social, streaming, gaming, messaging)
- Per-user viewing-time analytics stored in PostgreSQL
- Web app manifest and PWA icons served via API paths for reverse-proxy compatibility

---

## Architecture

```
gatekeeper/
  main.py                  # Compatibility shim: re-exports app.main:app
  app/
    main.py                # Real app assembly: FastAPI instance, routers, startup/shutdown
    api/
      v1.py                # All REST endpoints
      ui.py                # UI file serving (HTML, icons, manifest)
    core/
      config.py            # Env var loading, users.json / services_config.json readers
      database.py          # PostgreSQL pool and viewing-event helpers (psycopg3)
    schemas/
      gatekeeper.py        # Pydantic request models
    services/
      adguard.py           # AdGuard Home HTTP client
      scheduler.py         # In-memory temporary-unblock job scheduler
  services_config.json     # Blockable service catalogue
  users.json               # User list
  assets/                  # Static PWA assets
  smoke_test.py            # Basic integration smoke test
  smoke_test_v2.py         # PostgreSQL logging smoke test
```

`main.py` at the repository root is a three-line compatibility shim:

```python
from app.main import app  # noqa: F401
```

All application logic lives under `app/`.  `app/main.py` creates the FastAPI instance,
mounts static files, registers routers, and wires up startup/shutdown lifecycle hooks.

---

## Prerequisites

- Python 3.11+
- AdGuard Home instance reachable over HTTP
- PostgreSQL database (for viewing-time logging)
- Python dependencies — install with `pip install -r requirements.txt` if present
  (no `requirements.txt` is committed to the repo), or use the one-liner:

  ```bash
  pip install fastapi uvicorn httpx "psycopg[binary]" psycopg-pool python-dotenv pydantic asyncpg starlette
  ```

---

## Required Environment Variables

Place these in a `.env` file in the `gatekeeper/` directory or export them directly.

| Variable              | Description                                      |
|-----------------------|--------------------------------------------------|
| `ADGUARD_URL`         | Base URL of AdGuard Home, e.g. `http://host:3000`|
| `ADGUARD_USER_NAME`   | AdGuard admin username                           |
| `ADGUARD_PASSWORD`    | AdGuard admin password                           |
| `ADGUARD_VALID_PIN`   | PIN required for all mutating API requests       |
| `SQL_SERVER`          | PostgreSQL host                                  |
| `SQL_DB`              | PostgreSQL database name                         |
| `SQL_USER`            | PostgreSQL username                              |
| `SQL_PWD`             | PostgreSQL password                              |
| `SQL_SCHEMA`          | PostgreSQL schema containing the `viewrecords` table |

---

## Config Files

### users.json

List of users shown in the UI for attributing unblock events.

```json
{
  "users": ["Alice", "Bob"]
}
```

Each entry may be a plain string or an object with a `"name"` key.

### services_config.json

Catalogue of blockable services surfaced by `GET /api/v1/services/blockable`.

```json
{
  "services": [
    { "id": "youtube", "name": "YouTube", "category": "streaming" }
  ]
}
```

---

## Running the Server

Run from the `gatekeeper/` directory:

```bash
cd gatekeeper
uvicorn main:app --reload
```

For production or custom host/port:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

The `--reload` flag enables auto-reload on code changes (development only).

---

## Endpoint Groups

### System

| Method | Path                            | Auth | Description                        |
|--------|---------------------------------|------|------------------------------------|
| GET    | `/health`                       | No   | Liveness check                     |
| POST   | `/api/v1/auth/verify-pin`       | No   | Validate admin PIN                 |
| POST   | `/api/v1/debug/temporary-jobs`  | PIN  | List active temporary-unblock jobs |

### Clients and Users

| Method | Path               | Auth | Description                          |
|--------|--------------------|------|--------------------------------------|
| GET    | `/api/v1/clients`  | No   | List all AdGuard persistent clients  |
| GET    | `/api/v1/users`    | No   | List users from users.json           |

### Blockable Services Catalogue

| Method | Path                              | Auth | Description                                  |
|--------|-----------------------------------|------|----------------------------------------------|
| GET    | `/api/v1/services/blockable`      | No   | Full catalogue; filter with `?category=...`  |

### Per-Client Service Blocking

| Method | Path                                                                       | Auth | Description                                      |
|--------|----------------------------------------------------------------------------|------|--------------------------------------------------|
| GET    | `/api/v1/clients/{client_id}/services/blocked`                             | No   | List blocked services for a client               |
| GET    | `/api/v1/clients/{client_id}/services/blocked/{service_id}`                | No   | Check if a specific service is blocked           |
| PUT    | `/api/v1/clients/{client_id}/services/blocked/{service_id}`                | PIN  | Permanently block a service                      |
| DELETE | `/api/v1/clients/{client_id}/services/blocked/{service_id}`                | PIN  | Permanently unblock a service                    |
| DELETE | `/api/v1/clients/{client_id}/services/blocked`                             | PIN  | Unblock all services for a client                |
| POST   | `/api/v1/clients/{client_id}/services/blocked/{service_id}/temporary-unblock` | PIN  | Unblock for N minutes, then auto-re-block    |

### Internet Isolation

| Method | Path                                            | Auth | Description                              |
|--------|-------------------------------------------------|------|------------------------------------------|
| GET    | `/api/v1/clients/{client_id}/internet/isolation`| No   | Check isolation state                    |
| PUT    | `/api/v1/clients/{client_id}/internet/isolation`| PIN  | Isolate client (block all internet)      |
| DELETE | `/api/v1/clients/{client_id}/internet/isolation`| PIN  | Restore full internet access             |

### Viewing Time Analytics

| Method | Path                                        | Auth | Description                                              |
|--------|---------------------------------------------|------|----------------------------------------------------------|
| GET    | `/api/v1/users/{user_name}/viewing-time`    | No   | Today's per-service viewing seconds; filter by device_id |

### UI Assets

| Method | Path                                  | Description                         |
|--------|---------------------------------------|-------------------------------------|
| GET    | `/`                                   | Serves index.html (SPA entry point) |
| GET    | `/api/v1/ui/manifest.webmanifest`     | PWA web app manifest (JSON)         |
| GET    | `/api/v1/ui/manifest`                 | Alias for proxies                   |
| GET    | `/api/v1/ui/icon-192.png`             | PWA icon 192x192                    |
| GET    | `/api/v1/ui/icon-512.png`             | PWA icon 512x512                    |
| GET    | `/api/v1/ui/icon-maskable-512.png`    | PWA maskable icon                   |
| GET    | `/api/v1/ui/icon.svg`                 | SVG icon                            |

Each icon endpoint also has a suffix-free alias (e.g. `/api/v1/ui/icon-192`).

---

## Smoke Tests

Two smoke test scripts are provided in the project root.  Both must be run from the
`gatekeeper/` directory with `.env` populated.  They use FastAPI's `TestClient` so
no running server is required.

**smoke_test.py** -- Basic integration test using TestClient; exercises core endpoints and AdGuard flows.

**smoke_test_v2.py** -- PostgreSQL logging smoke test.  Verifies that unblock and
re-block events are written to and closed in the `viewrecords` table correctly.
Patches AdGuard calls so it can run without a real AdGuard instance.

```bash
cd gatekeeper
python smoke_test.py
python smoke_test_v2.py
```
