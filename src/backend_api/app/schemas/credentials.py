"""gSage AI — Pydantic schemas for the user credentials keychain."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CredentialKind(str, enum.Enum):
    """Mirror of :class:`src.shared.models.user_credential.CredentialKind`."""

    BASIC = "basic"
    TOKEN = "token"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    CUSTOM = "custom"


# Required-field map per credential kind.
_REQUIRED_BY_KIND: dict[CredentialKind, tuple[str, ...]] = {
    CredentialKind.BASIC: ("username", "password"),
    CredentialKind.TOKEN: ("token",),
    CredentialKind.API_KEY: ("token",),
    CredentialKind.OAUTH2: ("token",),
    CredentialKind.CUSTOM: (),
}


class ToolLinkIn(BaseModel):
    """Inline tool link sent at credential creation or via dedicated endpoint."""

    tool_name: str = Field(..., min_length=1, max_length=120)
    is_active: bool = False


class ToolLinkOut(BaseModel):
    id: uuid.UUID
    credential_id: uuid.UUID
    tool_name: str
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CredentialBase(BaseModel):
    """Common writable fields for credentials.

    All sensitive values are accepted in plaintext over HTTPS; the service
    layer encrypts them before persisting via
    :class:`~src.shared.security.encryption.FieldEncryption`.
    """

    label: str = Field(..., min_length=1, max_length=100)
    kind: CredentialKind

    username: Optional[str] = Field(default=None, max_length=255)
    password: Optional[str] = Field(default=None, max_length=4096)
    domain: Optional[str] = Field(default=None, max_length=255)
    token: Optional[str] = Field(default=None, max_length=8192)
    refresh_token: Optional[str] = Field(default=None, max_length=8192)
    extra_fields: Optional[dict] = None
    token_expires_at: Optional[datetime] = None


class CredentialIn(CredentialBase):
    """Payload for ``POST /credentials`` — supports inline tool links."""

    tool_links: list[ToolLinkIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_required_by_kind(self) -> "CredentialIn":
        missing = [
            f for f in _REQUIRED_BY_KIND[self.kind]
            if not getattr(self, f, None)
        ]
        if missing:
            raise ValueError(
                f"kind={self.kind.value} requires field(s): {', '.join(missing)}"
            )
        return self


class CredentialUpdate(BaseModel):
    """Payload for ``PUT /credentials/{id}`` — all fields optional.

    Only fields explicitly provided are updated. Sensitive fields set to an
    empty string clear the stored value.
    """

    label: Optional[str] = Field(default=None, min_length=1, max_length=100)
    kind: Optional[CredentialKind] = None
    username: Optional[str] = Field(default=None, max_length=255)
    password: Optional[str] = Field(default=None, max_length=4096)
    domain: Optional[str] = Field(default=None, max_length=255)
    token: Optional[str] = Field(default=None, max_length=8192)
    refresh_token: Optional[str] = Field(default=None, max_length=8192)
    extra_fields: Optional[dict] = None
    token_expires_at: Optional[datetime] = None


class CredentialOut(BaseModel):
    """Safe read model — sensitive values (password / token / refresh_token /
    extra_fields values) are NEVER exposed.

    ``username`` and ``domain`` ARE returned in plaintext: they identify the
    account on the remote system and the user must be able to see / edit
    them in the UI without re-typing.  Boolean ``has_*`` flags still cover
    every encrypted field so the UI can render "•••••" placeholders for the
    truly sensitive ones.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    label: str
    kind: CredentialKind
    username: Optional[str] = None
    domain: Optional[str] = None
    has_username: bool
    has_password: bool
    has_domain: bool
    has_token: bool
    has_refresh_token: bool
    has_extra_fields: bool
    extra_fields_keys: list[str] = Field(
        default_factory=list,
        description="Keys of the encrypted extra_fields JSON object (values never returned).",
    )
    token_expires_at: Optional[datetime]
    last_used_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    tool_links: list[ToolLinkOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class AvailableToolOut(BaseModel):
    """Tool advertised as requiring a user credential — for UI dropdowns."""

    name: str
    summary: str
    category: str
    credential_schema: Optional[dict] = None
