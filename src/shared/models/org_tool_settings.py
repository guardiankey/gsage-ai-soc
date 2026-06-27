"""gSage AI — Per-org tool settings model (enable/disable, etc.)."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GSageOrgToolSettings(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-organization tool settings.

    Used to enable/disable tools for a specific organization.
    By default every tool is *enabled* — a row only exists when an admin
    explicitly disables a tool or namespace.
    """

    __tablename__ = "gsage_org_tool_settings"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Tool name or config_namespace (e.g. 'k8s_observe' or 'kubernetes')",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="When False, the tool is hidden from agents and cannot be called",
    )

    __table_args__ = (
        UniqueConstraint("org_id", "tool_name", name="uq_org_tool_settings"),
    )
