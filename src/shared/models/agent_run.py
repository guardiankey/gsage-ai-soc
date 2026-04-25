"""gSage AI — AgentRun model."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.tenant_session import GSageTenantSession


class GSageAgentRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Tracks a single Agno agent run within a tenant session.

    Each run corresponds to one turn in the conversation (one user message
    + one agent response). Dual-agent (maker + reviewer) runs produce two
    GSageAgentRun records sharing the same session_id.
    """

    __tablename__ = "gsage_agent_runs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_tenant_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agno_run_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="Agno run ID from agno_sessions.runs[]",
    )
    agent_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="maker",
        comment="Type of agent that executed this run: maker | reviewer",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        comment="Run lifecycle status: pending | running | completed | failed | timeout",
    )
    input_tokens: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of input tokens consumed",
    )
    output_tokens: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of output tokens generated",
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Execution duration in milliseconds",
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Error details if status is failed or timeout",
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship(
        "GSageOrganization",
        back_populates="agent_runs",
    )
    session: Mapped[GSageTenantSession] = relationship(
        "GSageTenantSession",
        back_populates="agent_runs",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'timeout')",
            name="ck_gsage_agent_runs_status",
        ),
        CheckConstraint(
            "agent_type IN ('maker', 'reviewer')",
            name="ck_gsage_agent_runs_agent_type",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageAgentRun("
            f"id={self.id}, session_id={self.session_id}, "
            f"agent_type={self.agent_type}, status={self.status})>"
        )
