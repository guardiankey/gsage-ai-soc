"""gSage AI — API key schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Valid interface values accepted at key creation time.
# "web" / "api" / "cli" cover all API-based clients.
# "email" / "telegram" / "whatsapp" / "slack" are reserved for internal workers.
_VALID_INTERFACES = {"web", "api", "cli", "email", "telegram", "whatsapp", "slack"}


class APIKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    environment: str = Field(default="live", pattern="^(live|test)$")
    scoped_permissions: list[str] = Field(default_factory=list)
    expires_in_days: int = Field(default=365, ge=1, le=365)
    rate_limit_per_minute: int = Field(default=10, ge=1, le=10_000)
    user_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Bind key to a specific user (personal key). Omit for org-level keys.",
    )
    interface: Optional[str] = Field(
        default=None,
        description=(
            "Access interface this key is bound to. Controls response formatting "
            "(system prompt channel instructions). "
            "Valid values: web, api, cli, email, telegram, whatsapp, slack. "
            "Defaults to 'api' for org-level keys and 'web' for personal keys when omitted."
        ),
    )

    def model_post_init(self, __context) -> None:  # type: ignore[override]
        if self.interface is not None and self.interface not in _VALID_INTERFACES:
            raise ValueError(
                f"interface must be one of {sorted(_VALID_INTERFACES)} or null"
            )


class APIKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    environment: str
    scoped_permissions: list[str]
    interface: Optional[str]
    expires_at: datetime
    is_active: bool
    last_used_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class APIKeyCreated(APIKeyOut):
    """Returned only at creation time — includes the raw key (shown once)."""

    raw_key: str


class APIKeyRevoke(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)
