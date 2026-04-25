"""gSage AI — Department model.

Departments are sub-units within an organization used to scope resources
(DataStores, Knowledge, ToolConfig, EmailAccounts, Sessions, etc.).

Every organization gets at least one "Default" department (is_default=True)
created automatically at org creation time.  The default department cannot
be deleted.

Hierarchy::

    Organization
      └── Department   ← this model
            └── Resources (DataStore, ToolConfig, etc.)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user_department import GSageUserDepartment


class GSageDepartment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Department — a sub-unit within an organization.

    Resources that can be scoped to a department:
    - DataStores (required: dept_id NOT NULL)
    - ToolConfig / ToolState / InterfaceProfile (override chain: dept > org > default)
    - EmailAccounts (dept_id nullable — shared across org when NULL)
    - Sessions, BackgroundTasks, IngestJobs, Files, ScheduledJobs, APIKeys
      (all dept_id nullable — org-wide when NULL)
    """

    __tablename__ = "gsage_departments"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="URL- and CLI-friendly identifier (lowercase, hyphens only).",
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    # Marks the auto-created default department (cannot be deleted).
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True for the auto-created 'Default' department. Protected from deletion.",
    )

    # ── Relationships ────────────────────────────────────────────────────────
    organization: Mapped["GSageOrganization"] = relationship(
        "GSageOrganization",
        back_populates="departments",
    )
    members: Mapped[List["GSageUserDepartment"]] = relationship(
        "GSageUserDepartment",
        back_populates="department",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_department_org_slug"),
        UniqueConstraint("org_id", "name", name="uq_department_org_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageDepartment(id={self.id}, org_id={self.org_id}, "
            f"slug={self.slug!r}, is_default={self.is_default})>"
        )
