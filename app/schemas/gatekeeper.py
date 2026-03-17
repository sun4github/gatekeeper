from pydantic import BaseModel, Field


class ClientMutationRequest(BaseModel):
    """For operations that may create or modify a client record."""
    pin: str
    client_name: str  # Friendly name used when auto-creating the client (e.g., Son-Pi)
    user_name: str = Field(..., min_length=1)


class TemporaryUnblockRequest(BaseModel):
    pin: str
    client_name: str
    user_name: str = Field(..., min_length=1)
    duration_minutes: int = Field(..., ge=1, le=120)


class IsolationRequest(BaseModel):
    """For internet isolation operations; client_id is supplied in the URL path."""
    pin: str
    user_name: str = Field(..., min_length=1)


class PinVerifyRequest(BaseModel):
    """For validating the UI PIN gate before any control screens are shown."""
    pin: str
