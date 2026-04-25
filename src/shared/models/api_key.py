"""gSage AI — API Key model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.tenant_session import GSageTenantSession
    from src.shared.models.user import GSageUser


class GSageAPIKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """API Key model for organization-based authentication.

    API keys have scoped permissions (subset of org permissions),
    expiration date (max 1 year), and rate limits.
    """

    __tablename__ = "gsage_api_keys"

    # Tenant isolation
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dept_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="NULL = org-level key; set = key scoped to a specific department.",
    )

    # Optional user binding (NULL = org-level key; set = personal key tied to a user)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="NULL for org-level keys; set for keys tied to a specific user",
    )

    # Key metadata
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Human-readable name for this API key",
    )
    key_prefix: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        index=True,
        comment="First 12 chars of raw key for fast UI display (e.g. gk_live_N7x2)",
    )
    key_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="SHA-256 hash of the raw API key (never store the raw key)",
    )
    environment: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="live",
        comment="Key environment: live | test",
    )

    # Permissions (subset of org permissions)
    scoped_permissions: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of permission tags this key has access to",
    )

    # Expiration and status
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Expiration date (max 1 year from creation)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Admin can revoke instantly",
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Interface this key is bound to — determines response formatting (system prompt).
    # Values: web | api | cli | email | telegram | whatsapp | slack | None (auto-resolved).
    # Never derived from a client-supplied header; always set server-side at key creation.
    # Resolution at auth time: db_key.interface → "api" (org key) / "web" (personal key).
    interface: Mapped[Optional[str]] = mapped_column(
        String(30),
        nullable=True,
        default=None,
        comment="Access interface bound to this key (web/api/cli/…). None = auto-resolved.",
    )

    # Rate limiting
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer,
        default=10,
        nullable=False,
        comment="Max requests per minute for this key",
    )

    # Audit
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship(
        "GSageOrganization",
        back_populates="api_keys",
    )
    user: Mapped[Optional[GSageUser]] = relationship(
        "GSageUser",
        foreign_keys=[user_id],
        back_populates="api_keys",
    )
    tenant_sessions: Mapped[list[GSageTenantSession]] = relationship(
        "GSageTenantSession",
        back_populates="api_key",
    )

    __table_args__ = (
        CheckConstraint(
            "environment IN ('live', 'test')",
            name="ck_gsage_api_keys_environment",
        ),
    )

    def __repr__(self) -> str:
        return f"<GSageAPIKey(id={self.id}, name={self.name}, org_id={self.org_id}, env={self.environment})>"

