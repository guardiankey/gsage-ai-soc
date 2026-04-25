"""gSage AI — UserOrganization model (N:N with role)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser


class GSageUserOrganization(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Membership record linking a user to an organization with a role.

    Replaces the direct org_id FK on GSageUser, enabling a user
    to belong to multiple organizations with different roles.
    """

    __tablename__ = "gsage_user_organizations"

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
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="member",
        comment="User role within the organization: owner | admin | member | viewer",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Relationships
    user: Mapped[GSageUser] = relationship(
        "GSageUser",
        back_populates="memberships",
    )
    organization: Mapped[GSageOrganization] = relationship(
        "GSageOrganization",
        back_populates="memberships",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "org_id", name="uq_gsage_user_organizations_user_org"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')",
            name="ck_gsage_user_organizations_role",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageUserOrganization("
            f"user_id={self.user_id}, org_id={self.org_id}, role={self.role})>"
        )
