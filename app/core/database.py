import os
import re
from datetime import datetime, timezone
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

_DB_POOL: Optional[AsyncConnectionPool] = None
_SQL_SCHEMA: str = ""


def _validate_sql_identifier(name: str) -> str:
    """Raise RuntimeError if name is not a safe SQL identifier (letters, digits, underscore)."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise RuntimeError(f"SQL_SCHEMA '{name}' is not a valid SQL identifier")
    return name


def _build_db_conninfo() -> str:
    """Build a libpq conninfo string from SQL_* env vars. Raises RuntimeError if any required var is missing."""
    required = ("SQL_USER", "SQL_PWD", "SQL_SERVER", "SQL_DB")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
    return (
        f"host={os.getenv('SQL_SERVER')} dbname={os.getenv('SQL_DB')}"
        f" user={os.getenv('SQL_USER')} password={os.getenv('SQL_PWD')}"
    )


def _get_viewrecords_table() -> str:
    """Return fully qualified table name for viewing records."""
    return f'"{_SQL_SCHEMA}".viewrecords'


async def _fetch_user_viewing_record(conn, user_name: str) -> Optional[tuple]:
    """
    Fetch a user's viewing record row (id, viewings) with row-level lock.
    Returns tuple of (row_id, viewings_dict) or None if not found.
    """
    tbl = _get_viewrecords_table()
    cur = await conn.execute(
        f'SELECT id, viewings FROM {tbl}'
        f' WHERE user_id = %s ORDER BY id DESC LIMIT 1 FOR UPDATE',
        (user_name,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def _ensure_device_service_path(viewings: dict, client_id: str, device_name: str, service_id: str) -> None:
    """
    Ensure the nested path devices[client_id][service_id] exists in viewings.
    Initializes device and services if needed. Mutates viewings in-place.
    """
    devices = viewings.setdefault("devices", {})
    device = devices.setdefault(client_id, {"device_name": device_name, "services": {}})
    device["device_name"] = device_name  # Update in case it changed
    device.setdefault("services", {}).setdefault(service_id, [])


async def _update_viewing_record(conn, row_id: int, viewings: dict) -> None:
    """Update a viewing record row with new viewings data."""
    tbl = _get_viewrecords_table()
    await conn.execute(
        f'UPDATE {tbl} SET viewings = %s, updated_at = now() WHERE id = %s',
        (Jsonb(viewings), row_id),
    )


async def db_append_viewing_event(
    user_name: str,
    client_id: str,
    device_name: str,
    service_id: str,
    requested_duration_minutes: Optional[int],
) -> None:
    """
    Log an unblock request by appending a new event to the viewing history.
    Creates a new user record if needed, or appends to the existing JSONB array.
    """
    if _DB_POOL is None:
        return

    now_utc = datetime.now(timezone.utc).isoformat()
    duration_label = (
        f"{requested_duration_minutes} minutes"
        if requested_duration_minutes is not None
        else "infinite"
    )

    new_event = {
        "unblock_started_at": now_utc,
        "requested_duration_minutes": requested_duration_minutes,
        "requested_duration_label": duration_label,
        "unblock_ended_at": None,
        "actual_duration_seconds": None,
    }

    tbl = _get_viewrecords_table()

    async with _DB_POOL.connection() as conn:
        async with conn.transaction():
            existing = await _fetch_user_viewing_record(conn, user_name)

            if existing is None:
                viewings = {
                    "version": 1,
                    "devices": {
                        client_id: {
                            "device_name": device_name,
                            "services": {service_id: [new_event]},
                        }
                    },
                }
                await conn.execute(
                    f'INSERT INTO {tbl} (user_id, viewings) VALUES (%s, %s)',
                    (user_name, Jsonb(viewings)),
                )
            else:
                row_id, viewings = existing
                viewings = dict(viewings) if viewings else {"version": 1, "devices": {}}
                _ensure_device_service_path(viewings, client_id, device_name, service_id)
                viewings["devices"][client_id]["services"][service_id].append(new_event)
                await _update_viewing_record(conn, row_id, viewings)


async def db_close_viewing_event(
    user_name: str,
    client_id: str,
    service_id: str,
) -> None:
    """
    Close the most recent open unblock event for a user/device/service.
    Updates unblock_ended_at and calculates actual_duration_seconds.
    """
    if _DB_POOL is None:
        return

    now_utc = datetime.now(timezone.utc)

    async with _DB_POOL.connection() as conn:
        async with conn.transaction():
            existing = await _fetch_user_viewing_record(conn, user_name)
            if existing is None:
                return

            row_id, viewings = existing
            viewings = dict(viewings) if viewings else {"version": 1, "devices": {}}

            events = (
                viewings.get("devices", {})
                .get(client_id, {})
                .get("services", {})
                .get(service_id, [])
            )
            if not events:
                return

            found_and_closed = False
            for event in reversed(events):
                if event.get("unblock_ended_at") is None:
                    event["unblock_ended_at"] = now_utc.isoformat()
                    started_str = event.get("unblock_started_at")
                    if started_str:
                        try:
                            started_dt = datetime.fromisoformat(started_str)
                            if started_dt.tzinfo is None:
                                started_dt = started_dt.replace(tzinfo=timezone.utc)
                            duration_secs = max(0, int((now_utc - started_dt).total_seconds()))
                            event["actual_duration_seconds"] = duration_secs
                        except (ValueError, TypeError):
                            pass
                    found_and_closed = True
                    break

            if not found_and_closed:
                return

            await _update_viewing_record(conn, row_id, viewings)


async def db_get_viewing_time_today(
    user_name: str, device_id: Optional[str] = None
) -> dict:
    """
    Aggregate today's closed-session viewing seconds per service for a user.
    If device_id is provided, only that device's events are included;
    otherwise all devices are summed.
    Returns dict mapping service_id -> total_seconds (int).
    """
    if _DB_POOL is None:
        return {}

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    async with _DB_POOL.connection() as conn:
        tbl = _get_viewrecords_table()
        cur = await conn.execute(
            f'SELECT viewings FROM {tbl}'
            f' WHERE user_id = %s ORDER BY id DESC LIMIT 1',
            (user_name,),
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return {}

        viewings = row[0]
        devices = viewings.get("devices", {})
        totals: dict[str, int] = {}

        device_keys = [device_id] if device_id else list(devices.keys())
        for dk in device_keys:
            dev = devices.get(dk)
            if not dev:
                continue
            services = dev.get("services", {})
            for svc_id, events in services.items():
                if not isinstance(events, list):
                    continue
                for ev in events:
                    ended = ev.get("unblock_ended_at")
                    if not ended:
                        continue
                    secs = ev.get("actual_duration_seconds")
                    if not isinstance(secs, (int, float)) or secs <= 0:
                        continue
                    if ended < today_start:
                        continue
                    totals[svc_id] = totals.get(svc_id, 0) + int(secs)

        return totals


async def init_db_pool() -> None:
    """Initialise PostgreSQL connection pool and ensure schema/table exist."""
    global _DB_POOL, _SQL_SCHEMA
    schema_raw = os.getenv("SQL_SCHEMA", "").strip()
    if not schema_raw:
        raise RuntimeError("Missing required env var: SQL_SCHEMA")
    _SQL_SCHEMA = _validate_sql_identifier(schema_raw)
    _DB_POOL = AsyncConnectionPool(
        conninfo=_build_db_conninfo(),
        min_size=1,
        max_size=10,
        open=False,
    )
    await _DB_POOL.open()
    async with _DB_POOL.connection() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{_SQL_SCHEMA}"')
        await conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{_SQL_SCHEMA}".viewrecords ('
            f'  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,'
            f'  user_id TEXT NOT NULL,'
            f'  viewings JSONB,'
            f'  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()'
            f')'
        )
    print("Database connection pool established and schema/table ensured.")


async def close_db_pool() -> None:
    """Close the PostgreSQL connection pool."""
    global _DB_POOL
    if _DB_POOL is not None:
        await _DB_POOL.close(timeout=5)
