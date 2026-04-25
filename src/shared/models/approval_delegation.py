"""gSage AI — ApprovalDelegation model.

Records the concrete delegation for each Agno approval that was matched to a
rule.  Stores the context needed to resume the paused agent run under the
original requester's identity after the delegated approver resolves it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GSageApprovalDelegation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Concrete delegation record for a single Agno approval.

    Created in :mod:`src.backend_api.app.api.v1.chat` when a run is paused and
    a matching :class:`GSageApprovalRule` is found.  Used by the approvals
    router to:

    * Let the delegated approver see and resolve approvals they didn't originate.
    * Resume the agent run under the original requester's context.
    """

    __tablename__ = "gsage_approval_delegations"

    # The Agno approval this delegation is for (agno_approvals.id)
    approval_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        comment="References agno_approvals.id",
    )

    # The rule that produced this delegation (nullable in case rule was deleted)
    rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_approval_rules.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Tenant context
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
        comment="Department context at time of delegation.",
    )

    # User who triggered the tool call (run owner in Agno)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # User who must approve — secondary principal for this approval
    approver_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Tool info for display / audit
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Session / run context — needed to rebuild the agent for acontinue_run
    agno_session_id: Mapped[str] = mapped_column(String(500), nullable=False)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # Agent-generated human-readable description of the pending action
    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable summary injected by the agent via _approval_summary param",
    )

    # Set once the notification email is sent
    notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Set the first time acontinue_run() is called for this approval.
    # Subsequent calls to /continue-run will return 409 to avoid duplicate runs.
    continued_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set when acontinue_run() is first dispatched for this approval.",
    )

    # Relationships (lazy load — keep import-time cost low)
    rule = relationship("GSageApprovalRule", foreign_keys=[rule_id], lazy="select")
    requester = relationship("GSageUser", foreign_keys=[requester_user_id], lazy="select")
    approver = relationship("GSageUser", foreign_keys=[approver_user_id], lazy="select")
    organization = relationship("GSageOrganization", foreign_keys=[org_id], lazy="select")

    __table_args__ = (
        Index("ix_approval_delegation_approver", "approver_user_id"),
        Index("ix_approval_delegation_org", "org_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageApprovalDelegation("
            f"approval_id={self.approval_id}, "
            f"approver={self.approver_user_id}, "
            f"tool={self.tool_name})>"
        )
