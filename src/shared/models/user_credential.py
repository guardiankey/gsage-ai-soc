"""gSage AI — User credential keychain models.

Stores per-user credentials (basic / token / api_key / oauth2 / custom)
encrypted with AES-256-GCM via :func:`get_encryption`, plus N:N links to
tool names with a partial-unique constraint enforcing one active
credential per ``(user_id, tool_name)``.
"""

from __future__ import annotations

import enum
import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.shared.security.encryption import get_encryption

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser


class CredentialKind(str, enum.Enum):
    """Supported credential kinds for the user keychain."""

    BASIC = "basic"
    TOKEN = "token"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    CUSTOM = "custom"


CREDENTIAL_KIND_VALUES = tuple(k.value for k in CredentialKind)


class GSageUserCredential(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-user credential stored in the keychain.

    Sensitive fields are stored encrypted (AES-256-GCM, see
    :class:`~src.shared.security.encryption.FieldEncryption`) and exposed
    through ``@property`` accessors that decrypt on read and encrypt on
    write. Empty string clears the stored value.
    """

    __tablename__ = "gsage_user_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Denormalised for multi-tenant filtering — credential is scoped to (user, org).",
    )

    label: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Friendly name chosen by the user (e.g. 'AD Corp').",
    )
    kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Credential kind: basic | token | api_key | oauth2 | custom.",
    )

    _username_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "username_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted username.",
    )
    _password_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "password_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted password.",
    )
    _domain_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "domain_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted domain / tenant.",
    )
    _token_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "token_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted access / API token.",
    )
    _refresh_token_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "refresh_token_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted OAuth2 refresh token.",
    )
    _extra_fields_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "extra_fields_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted JSON blob of additional fields.",
    )

    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="OAuth2 / token expiration (informational — no auto-refresh).",
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    tool_links: Mapped[List["GSageUserCredentialToolLink"]] = relationship(
        "GSageUserCredentialToolLink",
        back_populates="credential",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_gsage_user_credentials_user_label"),
        CheckConstraint(
            f"kind IN ({', '.join(repr(v) for v in CREDENTIAL_KIND_VALUES)})",
            name="ck_gsage_user_credentials_kind",
        ),
    )

    # ── Encrypted field accessors ────────────────────────────────────────

    @staticmethod
    def _enc(value: Optional[str]) -> Optional[bytes]:
        if not value:
            return None
        return get_encryption().encrypt(value)

    @staticmethod
    def _dec(blob: Optional[bytes]) -> Optional[str]:
        if not blob:
            return None
        return get_encryption().decrypt(blob)

    @property
    def username(self) -> Optional[str]:
        return self._dec(self._username_encrypted)

    @username.setter
    def username(self, value: Optional[str]) -> None:
        self._username_encrypted = self._enc(value)

    @property
    def password(self) -> Optional[str]:
        return self._dec(self._password_encrypted)

    @password.setter
    def password(self, value: Optional[str]) -> None:
        self._password_encrypted = self._enc(value)

    @property
    def domain(self) -> Optional[str]:
        return self._dec(self._domain_encrypted)

    @domain.setter
    def domain(self, value: Optional[str]) -> None:
        self._domain_encrypted = self._enc(value)

    @property
    def token(self) -> Optional[str]:
        return self._dec(self._token_encrypted)

    @token.setter
    def token(self, value: Optional[str]) -> None:
        self._token_encrypted = self._enc(value)

    @property
    def refresh_token(self) -> Optional[str]:
        return self._dec(self._refresh_token_encrypted)

    @refresh_token.setter
    def refresh_token(self, value: Optional[str]) -> None:
        self._refresh_token_encrypted = self._enc(value)

    @property
    def extra_fields(self) -> Optional[dict]:
        raw = self._dec(self._extra_fields_encrypted)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    @extra_fields.setter
    def extra_fields(self, value: Optional[dict]) -> None:
        if not value:
            self._extra_fields_encrypted = None
        else:
            self._extra_fields_encrypted = get_encryption().encrypt(json.dumps(value))

    # ── Helpers ──────────────────────────────────────────────────────────

    def has_field(self, name: str) -> bool:
        """Return True when the underlying encrypted column for *name* is non-null."""
        attr = f"_{name}_encrypted"
        return bool(getattr(self, attr, None))

    def to_runtime_dict(self) -> dict:
        """Return decrypted credential as a dict — for injection into ``AgentContext``."""
        return {
            "username": self.username,
            "password": self.password,
            "domain": self.domain,
            "token": self.token,
            "refresh_token": self.refresh_token,
            "extra_fields": self.extra_fields,
            "token_expires_at": self.token_expires_at,
            "kind": self.kind,
            "label": self.label,
        }

    def __repr__(self) -> str:
        return (
            f"<GSageUserCredential(id={self.id}, user_id={self.user_id}, "
            f"label={self.label!r}, kind={self.kind})>"
        )


class GSageUserCredentialToolLink(Base, UUIDPrimaryKeyMixin):
    """N:N link between a credential and a tool name.

    A partial unique index enforces a single ``is_active=true`` row per
    ``(user_id, tool_name)`` pair.
    """

    __tablename__ = "gsage_user_credential_tool_links"

    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_user_credentials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Redundant with credential.user_id — denormalised for fast lookups.",
    )
    tool_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    credential: Mapped[GSageUserCredential] = relationship(
        "GSageUserCredential",
        back_populates="tool_links",
    )

    __table_args__ = (
        UniqueConstraint(
            "credential_id", "tool_name",
            name="uq_gsage_user_credential_tool_links_cred_tool",
        ),
        Index(
            "uq_gsage_user_credential_active_per_tool",
            "user_id", "tool_name",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageUserCredentialToolLink(id={self.id}, credential_id={self.credential_id}, "
            f"tool_name={self.tool_name!r}, is_active={self.is_active})>"
        )
