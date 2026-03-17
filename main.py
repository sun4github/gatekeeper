# Compatibility shim: allows `uvicorn main:app` to work from the gatekeeper/ directory.
# All application logic lives under app/.
from app.main import app  # noqa: F401
