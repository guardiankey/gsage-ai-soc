"""gSage AI — User-Department membership model.

Maps users to departments within an organization, carrying a role
(admin | member | viewer).  A user may belong to multiple departments
inside the same organization.

Role semantics
--------------
admin  — can manage department settings, members, and all resources.
member — default role; can create/read/update own resources within dept.
viewer — read-only access to shared resources within dept.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.department import GSageDepartment
    from src.shared.models.user import GSageUser


class DepartmentRole(StrEnum):
    """Roles a user can hold in a department."""

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class GSageUserDepartment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Association between a user and a department.

    Many-to-many between :class:`GSageUser` and
    :class:`GSageDepartment`, with additional role and activation state.
    """

    __tablename__ = "gsage_user_departments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DepartmentRole.MEMBER,
        comment="admin | member | viewer",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # ── Relationships ────────────────────────────────────────────────────────
    user: Mapped["GSageUser"] = relationship(
        "GSageUser",
        back_populates="department_memberships",
    )
    department: Mapped["GSageDepartment"] = relationship(
        "GSageDepartment",
        back_populates="members",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "dept_id", name="uq_user_department"),
        CheckConstraint(
            "role IN ('admin', 'member', 'viewer')",
            name="ck_user_department_role",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageUserDepartment(user_id={self.user_id}, "
            f"dept_id={self.dept_id}, role={self.role!r}, active={self.is_active})>"
        )
