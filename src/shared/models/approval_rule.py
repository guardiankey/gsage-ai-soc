"""gSage AI — ApprovalRule model.

Defines which user (approver) must approve tool calls matching a pattern of
(org_id, user_id, tool_name).  Each field can be a concrete UUID string or
the wildcard ``"*"``.

Specificity scoring is handled by :func:`src.shared.services.approval_rule_service.find_approver`.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GSageApprovalRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Approval delegation rule.

    Patterns can be a UUID string or ``"*"`` (wildcard).
    The most specific matching rule wins (each non-wildcard field adds +2 to
    the score; ties are broken by ``priority`` descending).
    ``dept_id_pattern`` non-wildcard also adds +2 to specificity score.
    """

    __tablename__ = "gsage_approval_rules"

    # Pattern fields — UUID string or "*"
    org_id_pattern: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="UUID string or '*'",
    )
    dept_id_pattern: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="*",
        server_default="*",
        comment="UUID string or '*' (wildcard matches any dept)",
    )
    user_id_pattern: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="UUID string or '*'",
    )
    tool_pattern: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Exact tool name or '*'",
    )

    # The user that must approve matching calls
    approver_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)

    # Higher priority wins when specificity scores are equal
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "org_id_pattern",
            "dept_id_pattern",
            "user_id_pattern",
            "tool_pattern",
            name="uq_approval_rule_patterns",
        ),
        Index("ix_approval_rule_active", "is_active"),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageApprovalRule("
            f"org={self.org_id_pattern}, "
            f"user={self.user_id_pattern}, "
            f"tool={self.tool_pattern}, "
            f"approver={self.approver_user_id})>"
        )
