"""gSage AI — TenantSession model."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.agent_run import GSageAgentRun
    from src.shared.models.api_key import GSageAPIKey
    from src.shared.models.email_thread import GSageEmailThread
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser


class GSageTenantSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Maps an Agno session to a tenant context (org + user or API key).

    One GSageTenantSession corresponds to one Agno session (agno_session_id).
    The session can be initiated by a human user (JWT) or a service account
    (API key). Exactly one of user_id or api_key_id must be set.
    """

    __tablename__ = "gsage_tenant_sessions"

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
        comment="Active department at session creation. NULL = session not dept-scoped.",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Set when session is initiated via JWT (human user)",
    )
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Set when session is initiated via API key (service account)",
    )
    agno_session_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="Agno session ID (foreign key into agno_sessions table)",
    )
    title: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment="Human-readable session title (auto-generated from first message or user-defined)",
    )
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="web",
        server_default="web",
        comment="Channel that initiated this session: web | cli | api | email | scheduled",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship(
        "GSageOrganization",
        back_populates="tenant_sessions",
    )
    user: Mapped[Optional[GSageUser]] = relationship(
        "GSageUser",
        back_populates="tenant_sessions",
    )
    api_key: Mapped[Optional[GSageAPIKey]] = relationship(
        "GSageAPIKey",
        back_populates="tenant_sessions",
    )
    agent_runs: Mapped[List[GSageAgentRun]] = relationship(
        "GSageAgentRun",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    email_threads: Mapped[List["GSageEmailThread"]] = relationship(
        "GSageEmailThread",
        back_populates="session",
    )

    def __repr__(self) -> str:
        return (
            f"<GSageTenantSession("
            f"id={self.id}, agno_session_id={self.agno_session_id}, org_id={self.org_id})>"
        )
