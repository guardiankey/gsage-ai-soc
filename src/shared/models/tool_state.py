"""gSage AI — Tool runtime state model (per-org)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization


class GSageToolState(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-organization tool runtime state.

    Stores tool runtime data (usage counters, quotas, rate limit windows, etc.).
    One state row per (org, tool, profile).  NOT encrypted (operational counters
    only).  Single-config tools always use ``profile_id = 'default'``.
    """

    __tablename__ = "gsage_tool_state"

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
        comment="NULL = org-wide state; set = department-specific state.",
    )

    # Tool identification
    tool_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Matches tool registry name",
    )

    # Config profile this state belongs to
    profile_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="default",
        server_default="default",
        comment="Config profile this state belongs to. "
                "Mirrors GSageToolConfig.profile_id.",
    )

    # Runtime state (JSONB, NOT encrypted)
    state: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Tool-managed runtime state (counters, quotas, timestamps)",
    )

    # Reset policy
    reset_policy: Mapped[str] = mapped_column(
        String(20),
        default="never",
        nullable=False,
        comment="When to auto-reset: daily, monthly, never",
    )
    last_reset_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last time state was reset by scheduled task",
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship("GSageOrganization")

    __table_args__ = (
        UniqueConstraint(
            "org_id", "dept_id", "tool_name", "profile_id",
            name="uq_tool_state_org_dept_tool_profile",
        ),
        # Partial unique index for org-wide state rows (dept_id IS NULL).
        # Required so that ON CONFLICT (org_id, tool_name, profile_id) WHERE
        # dept_id IS NULL works correctly, since the full unique constraint
        # above cannot enforce uniqueness when dept_id is NULL (NULL != NULL
        # in PostgreSQL unique constraints).
        Index(
            "uix_tool_state_org_tool_profile_no_dept",
            "org_id", "tool_name", "profile_id",
            unique=True,
            postgresql_where="dept_id IS NULL",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageToolState(id={self.id}, org_id={self.org_id}, "
            f"tool_name={self.tool_name}, profile_id={self.profile_id})>"
        )
