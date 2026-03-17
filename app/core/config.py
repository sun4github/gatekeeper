import json
import os

from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv(override=True)

ADGUARD_URL: str = os.getenv("ADGUARD_URL", "")
AUTH: tuple[str, str] = (os.getenv("ADGUARD_USER_NAME", ""), os.getenv("ADGUARD_PASSWORD", ""))
VALID_PIN: str = os.getenv("ADGUARD_VALID_PIN", "")

with open("services_config.json") as _f:
    BLOCKABLE_SERVICES: list = json.load(_f).get("services", [])


def load_users() -> list:
    """Load and validate users from users.json. Raises HTTPException on failure."""
    try:
        with open("users.json") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read users.json: {exc}")
    raw = data.get("users", [])
    users = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            users.append({"name": entry.strip()})
        elif isinstance(entry, dict) and entry.get("name"):
            users.append({"name": str(entry["name"])})
    if not users:
        raise HTTPException(status_code=500, detail="users.json contains no valid users")
    return users
