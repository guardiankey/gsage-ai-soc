"""gSage AI — Permission model (tag-based RBAC)."""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.group import GSageGroup


class GSagePermission(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Permission model with tag-based access control.

    Permissions are global (not org-scoped) and assigned to groups.
    Examples: "dns:read", "whois:read", "decode:base64", "tool_config:write"
    """

    __tablename__ = "gsage_permissions"

    # Permission tag (e.g., "dns:read", "network:scan")
    tag: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        comment="Permission tag (e.g., dns:read, tool_config:write)",
    )

    # Description
    description: Mapped[str] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable description of this permission",
    )

    # Category for UI grouping
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="tool",
        comment="Category: tool, admin, network, threat, etc.",
    )

    # Relationships
    groups: Mapped[List[GSageGroup]] = relationship(
        "GSageGroup",
        secondary="gsage_group_permissions",
        back_populates="permissions",
    )

    def __repr__(self) -> str:
        return f"<GSagePermission(id={self.id}, tag={self.tag})>"
