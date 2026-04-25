"""gSage AI — Group model (RBAC)."""

from __future__ import annotations

from typing import TYPE_CHECKING, List
import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.permission import GSagePermission
    from src.shared.models.user import GSageUser


# ---------------------------------------------------------------------------
# Association table: GSageGroup <-> GSagePermission (with dept scoping)
#
# dept_id = NULL  → permission applies in all departments (global)
# dept_id = <uuid> → permission applies only in that department
#
# The surrogate UUID primary key is required because the same
# (group_id, permission_id) pair may exist with different dept_id values,
# and NULL cannot participate in a composite primary key.
#
# Uniqueness is enforced via two partial indexes:
#   uq_group_perm_global  — (group_id, permission_id) WHERE dept_id IS NULL
#   uq_group_perm_dept    — (group_id, permission_id, dept_id) WHERE dept_id IS NOT NULL
# ---------------------------------------------------------------------------
from sqlalchemy import Table, Column

gsage_group_permissions = Table(
    "gsage_group_permissions",
    Base.metadata,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    ),
    Column(
        "group_id",
        UUID(as_uuid=True),
        ForeignKey("gsage_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column(
        "permission_id",
        UUID(as_uuid=True),
        ForeignKey("gsage_permissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column(
        "dept_id",
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    ),
    # Partial unique indexes — defined after table creation via Index objects
)

# Enforce at most one global assignment per (group, permission) pair
Index(
    "uq_group_perm_global",
    gsage_group_permissions.c.group_id,
    gsage_group_permissions.c.permission_id,
    unique=True,
    postgresql_where=gsage_group_permissions.c.dept_id.is_(None),
)

# Enforce at most one dept-scoped assignment per (group, permission, dept) triple
Index(
    "uq_group_perm_dept",
    gsage_group_permissions.c.group_id,
    gsage_group_permissions.c.permission_id,
    gsage_group_permissions.c.dept_id,
    unique=True,
    postgresql_where=gsage_group_permissions.c.dept_id.isnot(None),
)


class GSageGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Group model for RBAC (role-based access control)."""

    __tablename__ = "gsage_groups"

    # Tenant isolation
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Group info
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)

    # Relationships
    organization: Mapped[GSageOrganization] = relationship(
        "GSageOrganization",
        back_populates="groups",
    )
    users: Mapped[List[GSageUser]] = relationship(
        "GSageUser",
        secondary="gsage_user_groups",
        back_populates="groups",
    )
    permissions: Mapped[List[GSagePermission]] = relationship(
        "GSagePermission",
        secondary=gsage_group_permissions,
        back_populates="groups",
    )

    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_groups_org_name"),
    )

    def __repr__(self) -> str:
        return f"<GSageGroup(id={self.id}, name={self.name}, org_id={self.org_id})>"
