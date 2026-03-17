from typing import Optional

import httpx

from app.core.config import ADGUARD_URL, AUTH


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


async def list_persistent_clients_raw() -> list:
    """Fetch all persistent clients from AdGuard. Read-only, no side effects."""
    async with httpx.AsyncClient(auth=AUTH) as http:
        resp = await http.get(f"{ADGUARD_URL}/clients")
        resp.raise_for_status()
        return resp.json().get("clients", [])
