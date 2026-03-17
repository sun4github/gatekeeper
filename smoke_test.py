#!/usr/bin/env python3
"""
Smoke test for PostgreSQL logging behavior.
Tests unblock logging and block-closure mutation end-to-end without AdGuard.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from typing import Optional

# Load env vars FIRST, before any imports
import dotenv
dotenv.load_dotenv(dotenv_path="/home/suneel/repos/gatekeeper/.env", override=True)

import asyncpg
import httpx

# Import app after env vars are loaded
from main import app, on_startup_init_db, on_shutdown_cleanup_jobs

# ─── TEST CONFIGURATION ───────────────────────────────────────────────────────

TEST_CLIENT_ID = "smoke-test-client"
TEST_SERVICE_ID = "youtube"
TEST_USER_NAME = "test_user"
TEST_CLIENT_NAME = "SmokeLab"
TEST_PIN = os.getenv("ADGUARD_VALID_PIN", "6789")

# ─── DATABASE HELPERS ─────────────────────────────────────────────────────────

async def get_db_pool() -> asyncpg.Pool:
    """Build and return a direct asyncpg connection to test DB."""
    user = os.getenv("SQL_USER")
    pwd = os.getenv("SQL_PWD")
    server = os.getenv("SQL_SERVER")
    db = os.getenv("SQL_DB")
    schema = os.getenv("SQL_SCHEMA")
    
    if not all([user, pwd, server, db, schema]):
        raise RuntimeError(f"Missing DB env vars: user={user}, pwd={'***'}, server={server}, db={db}, schema={schema}")
    
    dsn = f"postgresql://{user}:{pwd}@{server}/{db}"
    
    async def init_conn(conn):
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10, init=init_conn)


async def query_user_viewings(pool: asyncpg.Pool, user_name: str) -> Optional[dict]:
    """Query the viewings JSONB for a user from PostgreSQL."""
    schema = os.getenv("SQL_SCHEMA")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f'SELECT viewings FROM "{schema}"."viewRecords" WHERE user_id = $1 ORDER BY id DESC LIMIT 1',
            user_name
        )
        return row["viewings"] if row else None


async def cleanup_user_record(pool: asyncpg.Pool, user_name: str) -> None:
    """Delete test user records from DB."""
    schema = os.getenv("SQL_SCHEMA")
    async with pool.acquire() as conn:
        await conn.execute(
            f'DELETE FROM "{schema}"."viewRecords" WHERE user_id = $1',
            user_name
        )


# ─── MOCK SETUP ───────────────────────────────────────────────────────────────

async def mock_set_service_block_state(client_id: str, client_name: str, service: str, blocked: bool) -> None:
    """No-op mock for AdGuard service blocking."""
    print(f"  [MOCK] set_service_block_state({client_id}, {client_name}, {service}, blocked={blocked})")


async def mock_cancel_temporary_unblock_job(client_id: str, service: str) -> bool:
    """Mock cancellation return; no-op."""
    print(f"  [MOCK] cancel_temporary_unblock_job({client_id}, {service}) -> False")
    return False


async def mock_schedule_temporary_unblock(
    client_id: str, client_name: str, service: str, duration_minutes: int, user_name: str = ""
) -> bool:
    """Mock scheduling; returns False (no existing job to replace)."""
    print(f"  [MOCK] schedule_temporary_unblock(..., duration_minutes={duration_minutes}, user_name={user_name}) -> False")
    return False


# ─── TEST STEPS ───────────────────────────────────────────────────────────────

def _send_request_with_body(client, method: str, url: str, json_data: dict):
    """Helper to send requests with JSON body for DELETE/PUT methods."""
    body = json.dumps(json_data)
    return client.request(method.upper(), url, content=body, headers={"content-type": "application/json"})


async def run_smoke_test():
    """Main smoke test orchestration."""
    print("\n" + "="*80)
    print("GATEKEEPER POSTGRESQL LOGGING SMOKE TEST")
    print("="*80)
    
    results = {
        "checks": [],
        "errors": []
    }
    
    try:
        # Step 1: Initialize database explicitly
        print("[1] Initializing database pool...")
        await on_startup_init_db()
        
        from main import _DB_POOL
        if _DB_POOL is None:
            results["checks"].append(("DB startup handlers completed", "FAIL", "_DB_POOL is still None after init"))
            results["errors"].append("CRITICAL: on_startup_init_db() did not initialize _DB_POOL")
            return results
        
        results["checks"].append(("DB startup handlers completed", "PASS", None))
        print("    ✓ Database initialized (pool is set)")
        
        # Step 2: Create test client
        print("\n[2] Creating FastAPI test client...")
        from starlette.testclient import TestClient as StarletteTestClient
        client = StarletteTestClient(app)
        results["checks"].append(("FastAPI test client created", "PASS", None))
        print("    ✓ Test client ready")
        
        # Step 3: Verify PIN endpoint works
        print("\n[2] Verifying PIN validation...")
        resp = _send_request_with_body(client, "POST", "/api/v1/auth/verify-pin", {"pin": TEST_PIN})
        if resp.status_code == 200:
            results["checks"].append(("PIN validation endpoint", "PASS", None))
            print(f"    ✓ PIN accepted: {resp.json()}")
        else:
            results["checks"].append(("PIN validation endpoint", "FAIL", f"Status {resp.status_code}"))
            print(f"    ✗ PIN validation failed: {resp.status_code}")
            return results
        
        # Step 4: Get DB pool for direct queries
        print("\n[3] Getting database connection pool...")
        db_pool = await get_db_pool()
        results["checks"].append(("DB connection pool acquired", "PASS", None))
        print("    ✓ Pool ready")
        
        # Step 5: Clean up any existing test records
        print("\n[4] Cleaning up stale test records...")
        await cleanup_user_record(db_pool, TEST_USER_NAME)
        print(f"    ✓ Cleaned records for {TEST_USER_NAME}")
        
        # ───────────────────────────────────────────────────────────────────────
        # PATCH AdGuard functions and test permanent unblock
        # ───────────────────────────────────────────────────────────────────────
        print("\n" + "-"*80)
        print("SCENARIO A: Permanent Unblock (DELETE endpoint)")
        print("-"*80)
        
        with patch("main.set_service_block_state", side_effect=mock_set_service_block_state):
            with patch("main.cancel_temporary_unblock_job", side_effect=mock_cancel_temporary_unblock_job):
                print("\n[5a] Calling DELETE /api/v1/clients/{client_id}/services/blocked/{service_id}...")
                unblock_payload = {
                    "pin": TEST_PIN,
                    "client_name": TEST_CLIENT_NAME,
                    "user_name": TEST_USER_NAME
                }
                resp = _send_request_with_body(client, "DELETE",
                    f"/api/v1/clients/{TEST_CLIENT_ID}/services/blocked/{TEST_SERVICE_ID}",
                    unblock_payload
                )
                if resp.status_code == 200:
                    results["checks"].append(("Permanent unblock endpoint call", "PASS", None))
                    print(f"    ✓ Status {resp.status_code}: {resp.json()}")
                else:
                    results["checks"].append(("Permanent unblock endpoint call", "FAIL", f"Status {resp.status_code}"))
                    results["errors"].append(f"Unblock failed: {resp.text}")
                    print(f"    ✗ Status {resp.status_code}: {resp.text}")
        
        # Step 6b: Query and verify unblock event was logged
        print("\n[5b] Querying PostgreSQL for unblock event...")
        await asyncio.sleep(0.5)  # Let the transaction settle
        viewings = await query_user_viewings(db_pool, TEST_USER_NAME)
        
        if viewings is None:
            results["checks"].append(("User record exists after unblock", "FAIL", "No record found"))
            results["errors"].append(f"After unblock: no viewings record for {TEST_USER_NAME}")
            print(f"    ✗ No viewings record found")
        else:
            results["checks"].append(("User record exists after unblock", "PASS", None))
            print(f"    ✓ Record found: {json.dumps(viewings, indent=2)}")
            
            # Validate unblock event structure
            devices = viewings.get("devices", {})
            service_events = devices.get(TEST_CLIENT_ID, {}).get("services", {}).get(TEST_SERVICE_ID, [])
            
            if not service_events:
                results["checks"].append(("Service events exist after unblock", "FAIL", "No events found"))
                results["errors"].append(f"No events for service {TEST_SERVICE_ID}")
                print(f"    ✗ No events found for service {TEST_SERVICE_ID}")
            else:
                latest_event = service_events[-1]
                print(f"    Latest event: {json.dumps(latest_event, indent=6)}")
                
                # Check requested_duration_label
                if latest_event.get("requested_duration_label") == "infinite":
                    results["checks"].append(("Unblock event: requested_duration_label='infinite'", "PASS", None))
                    print(f"    ✓ Duration label is 'infinite'")
                else:
                    results["checks"].append(
                        ("Unblock event: requested_duration_label='infinite'", "FAIL",
                         f"Got '{latest_event.get('requested_duration_label')}'")
                    )
                    print(f"    ✗ Got '{latest_event.get('requested_duration_label')}'")
                
                # Check unblock_ended_at is null (event is open)
                if latest_event.get("unblock_ended_at") is None:
                    results["checks"].append(("Unblock event: unblock_ended_at is null", "PASS", None))
                    print(f"    ✓ Event is open (unblock_ended_at = null)")
                else:
                    results["checks"].append(
                        ("Unblock event: unblock_ended_at is null", "FAIL",
                         f"Got {latest_event.get('unblock_ended_at')}")
                    )
                    print(f"    ✗ Got {latest_event.get('unblock_ended_at')}")
        
        # ───────────────────────────────────────────────────────────────────────
        # Test block endpoint (closes the unblock event)
        # ───────────────────────────────────────────────────────────────────────
        print("\n" + "-"*80)
        print("SCENARIO B: Block Service (PUT endpoint)")
        print("-"*80)
        
        with patch("main.set_service_block_state", side_effect=mock_set_service_block_state):
            with patch("main.cancel_temporary_unblock_job", side_effect=mock_cancel_temporary_unblock_job):
                print("\n[6a] Calling PUT /api/v1/clients/{client_id}/services/blocked/{service_id}...")
                block_payload = {
                    "pin": TEST_PIN,
                    "client_name": TEST_CLIENT_NAME,
                    "user_name": TEST_USER_NAME
                }
                resp = _send_request_with_body(client, "PUT",
                    f"/api/v1/clients/{TEST_CLIENT_ID}/services/blocked/{TEST_SERVICE_ID}",
                    block_payload
                )
                if resp.status_code == 200:
                    results["checks"].append(("Block service endpoint call", "PASS", None))
                    print(f"    ✓ Status {resp.status_code}: {resp.json()}")
                else:
                    results["checks"].append(("Block service endpoint call", "FAIL", f"Status {resp.status_code}"))
                    results["errors"].append(f"Block failed: {resp.text}")
                    print(f"    ✗ Status {resp.status_code}: {resp.text}")
        
        # Step 7b: Query and verify the unblock event was closed
        print("\n[6b] Querying PostgreSQL to verify unblock event closure...")
        await asyncio.sleep(0.5)
        viewings = await query_user_viewings(db_pool, TEST_USER_NAME)
        
        if viewings is None:
            results["checks"].append(("Records persist after block", "FAIL", "No record found"))
            print(f"    ✗ Records were deleted")
        else:
            results["checks"].append(("Records persist after block", "PASS", None))
            
            service_events = (
                viewings.get("devices", {})
                .get(TEST_CLIENT_ID, {})
                .get("services", {})
                .get(TEST_SERVICE_ID, [])
            )
            
            if service_events:
                latest_event = service_events[-1]
                print(f"    Latest event after block: {json.dumps(latest_event, indent=6)}")
                
                # Check unblock_ended_at is populated
                if latest_event.get("unblock_ended_at") is not None:
                    results["checks"].append(("Unblock event: unblock_ended_at populated", "PASS", None))
                    print(f"    ✓ Event closed at {latest_event.get('unblock_ended_at')}")
                else:
                    results["checks"].append(
                        ("Unblock event: unblock_ended_at populated", "FAIL", "Still null")
                    )
                    print(f"    ✗ Still null after block")
                
                # Check actual_duration_seconds is populated
                if latest_event.get("actual_duration_seconds") is not None:
                    actual_duration = latest_event.get("actual_duration_seconds")
                    results["checks"].append(("Unblock event: actual_duration_seconds populated", "PASS", None))
                    print(f"    ✓ Duration calculated: {actual_duration} seconds")
                else:
                    results["checks"].append(
                        ("Unblock event: actual_duration_seconds populated", "FAIL", "Still null")
                    )
                    print(f"    ✗ Still null")
        
        # ───────────────────────────────────────────────────────────────────────
        # Test temporary unblock endpoint
        # ───────────────────────────────────────────────────────────────────────
        print("\n" + "-"*80)
        print("SCENARIO C: Temporary Unblock (POST endpoint)")
        print("-"*80)
        
        with patch("main.set_service_block_state", side_effect=mock_set_service_block_state):
            with patch("main.schedule_temporary_unblock", side_effect=mock_schedule_temporary_unblock):
                print("\n[7a] Calling POST /api/v1/clients/{client_id}/services/blocked/{service_id}/temporary-unblock...")
                temp_unblock_payload = {
                    "pin": TEST_PIN,
                    "client_name": TEST_CLIENT_NAME,
                    "user_name": TEST_USER_NAME,
                    "duration_minutes": 30
                }
                resp = _send_request_with_body(client, "POST",
                    f"/api/v1/clients/{TEST_CLIENT_ID}/services/blocked/{TEST_SERVICE_ID}/temporary-unblock",
                    temp_unblock_payload
                )
                if resp.status_code == 200:
                    results["checks"].append(("Temporary unblock endpoint call", "PASS", None))
                    print(f"    ✓ Status {resp.status_code}: {resp.json()}")
                else:
                    results["checks"].append(("Temporary unblock endpoint call", "FAIL", f"Status {resp.status_code}"))
                    results["errors"].append(f"Temp unblock failed: {resp.text}")
                    print(f"    ✗ Status {resp.status_code}: {resp.text}")
        
        # Step 8b: Query and verify temp unblock event was logged
        print("\n[7b] Querying PostgreSQL for temporary unblock event...")
        await asyncio.sleep(0.5)
        viewings = await query_user_viewings(db_pool, TEST_USER_NAME)
        
        if viewings is None:
            results["checks"].append(("Record exists after temp unblock", "FAIL", "No record found"))
            print(f"    ✗ No record found")
        else:
            results["checks"].append(("Record exists after temp unblock", "PASS", None))
            
            service_events = (
                viewings.get("devices", {})
                .get(TEST_CLIENT_ID, {})
                .get("services", {})
                .get(TEST_SERVICE_ID, [])
            )
            
            if service_events:
                latest_event = service_events[-1]
                print(f"    Latest event: {json.dumps(latest_event, indent=6)}")
                
                # Check requested_duration_minutes matches
                if latest_event.get("requested_duration_minutes") == 30:
                    results["checks"].append(("Temp unblock event: duration matches (30 minutes)", "PASS", None))
                    print(f"    ✓ Duration is 30 minutes")
                else:
                    results["checks"].append(
                        ("Temp unblock event: duration matches (30 minutes)", "FAIL",
                         f"Got {latest_event.get('requested_duration_minutes')}")
                    )
                    print(f"    ✗ Got {latest_event.get('requested_duration_minutes')} minutes")
                
                # Check requested_duration_label
                expected_label = "30 minutes"
                if latest_event.get("requested_duration_label") == expected_label:
                    results["checks"].append(("Temp unblock event: label='30 minutes'", "PASS", None))
                    print(f"    ✓ Label is '{expected_label}'")
                else:
                    results["checks"].append(
                        ("Temp unblock event: label='30 minutes'", "FAIL",
                         f"Got '{latest_event.get('requested_duration_label')}'")
                    )
                    print(f"    ✗ Got '{latest_event.get('requested_duration_label')}'")
                
                # Check unblock_ended_at is still null (event is open)
                if latest_event.get("unblock_ended_at") is None:
                    results["checks"].append(("Temp unblock event: unblock_ended_at is null", "PASS", None))
                    print(f"    ✓ Event is open (unblock_ended_at = null)")
                else:
                    results["checks"].append(
                        ("Temp unblock event: unblock_ended_at is null", "FAIL",
                         f"Got {latest_event.get('unblock_ended_at')}")
                    )
                    print(f"    ✗ Got {latest_event.get('unblock_ended_at')}")
        
        # ───────────────────────────────────────────────────────────────────────
        # Cleanup
        # ───────────────────────────────────────────────────────────────────────
        print("\n" + "-"*80)
        print("CLEANUP")
        print("-"*80)
        
        print("\n[8a] Cleaning up test database records...")
        await cleanup_user_record(db_pool, TEST_USER_NAME)
        print(f"    ✓ Deleted records for {TEST_USER_NAME}")
        
        print("\n[8b] Closing database pool...")
        await db_pool.close()
        print(f"    ✓ Pool closed")
        
        print("\n[8c] Running FastAPI shutdown handlers...")
        await on_shutdown_cleanup_jobs()
        print(f"    ✓ Shutdown complete")
        
        results["checks"].append(("Cleanup: test data deleted", "PASS", None))
        results["checks"].append(("Cleanup: database pool closed", "PASS", None))
        results["checks"].append(("Cleanup: shutdown handlers called", "PASS", None))
        
    except Exception as e:
        import traceback
        results["errors"].append(f"Unexpected error: {e}\n{traceback.format_exc()}")
        results["checks"].append(("Overall test execution", "FAIL", str(e)))
    
    return results


# ─── RESULT REPORTING ─────────────────────────────────────────────────────────

def print_results(results: dict):
    """Print test results summary."""
    print("\n\n" + "="*80)
    print("TEST RESULTS SUMMARY")
    print("="*80)
    
    checks = results.get("checks", [])
    errors = results.get("errors", [])
    
    pass_count = sum(1 for _, status, _ in checks if status == "PASS")
    fail_count = sum(1 for _, status, _ in checks if status == "FAIL")
    
    print(f"\nTotal checks: {len(checks)}")
    print(f"Passed: {pass_count}")
    print(f"Failed: {fail_count}")
    
    print("\n" + "-"*80)
    print("DETAILED CHECK RESULTS")
    print("-"*80)
    
    for check_name, status, detail in checks:
        icon = "✓" if status == "PASS" else "✗"
        print(f"\n{icon} {check_name}")
        if status == "FAIL":
            print(f"  Status: {status}")
            if detail:
                print(f"  Detail: {detail}")
    
    if errors:
        print("\n" + "-"*80)
        print("ERRORS")
        print("-"*80)
        for error in errors:
            print(f"\n  ✗ {error}")
    
    print("\n" + "="*80)
    if fail_count == 0 and not errors:
        print("OVERALL: PASS ✓")
    else:
        print("OVERALL: FAIL ✗")
    print("="*80 + "\n")
    
    return fail_count == 0 and not errors


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\nStarting smoke test...")
    print(f"Test configuration:")
    print(f"  PIN: {TEST_PIN}")
    print(f"  Client ID: {TEST_CLIENT_ID}")
    print(f"  Service ID: {TEST_SERVICE_ID}")
    print(f"  User Name: {TEST_USER_NAME}")
    print(f"  DB Schema: {os.getenv('SQL_SCHEMA')}")
    
    results = asyncio.run(run_smoke_test())
    success = print_results(results)
    
    sys.exit(0 if success else 1)
