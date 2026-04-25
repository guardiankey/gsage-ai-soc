"""gSage AI — Interface Profile model.

Defines per-organization, per-interface tool permission scoping and
configuration.  Each row represents an access interface (web, email,
telegram, whatsapp, slack, api, cli) and controls which tools are
available when requests arrive through that interface.

Permission modes:
    * **allowlist** — only the listed tool tags are permitted.
    * **denylist**  — all tags are permitted *except* the listed ones.

The final effective permission set is the **intersection** of the user's
own tag-based permissions and the interface filter.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser


class GSageInterfaceProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-org interface tool-permission profile.

    Examples::

        # Telegram — only allow read-only tools
        GSageInterfaceProfile(
            org_id=org.id,
            interface="telegram",
            mode="allowlist",
            tool_permissions=["dns:read", "whois:read", "knowledge:read"],
        )

        # Email — deny sending emails (prevent loops)
        GSageInterfaceProfile(
            org_id=org.id,
            interface="email",
            mode="denylist",
            tool_permissions=["email:send"],
        )
    """

    __tablename__ = "gsage_interface_profiles"
    __table_args__ = (
        # Partial unique indexes scoped by department.
        # One org-wide profile per (dept, channel) when user_id IS NULL.
        Index(
            "uq_interface_profile_dept_iface_org",
            "org_id", "dept_id", "interface",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        # One user-level profile per (dept, channel, user).
        Index(
            "uq_interface_profile_dept_iface_user",
            "org_id", "dept_id", "interface", "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    # FK to organisation (tenant)
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
        comment="NULL = org-wide profile; set = department-specific override.",
    )

    # Interface identifier — matches RequestSource values
    interface: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
        comment="Access interface: web, email, telegram, whatsapp, slack, api, cli",
    )

    # Optional user scoping — NULL means this profile applies org-wide
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="User-scoped profile; NULL means org-wide default",
    )

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable description of this interface profile",
    )

    # Channel-specific system prompt addition (appended after base/org/user prompts)
    system_prompt: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Channel-specific system prompt appended after base/org/user prompts",
    )

    # Permission filtering mode
    mode: Mapped[str] = mapped_column(
        String(10),
        default="denylist",
        nullable=False,
        comment="allowlist = only these tags; denylist = all except these tags",
    )

    # List of tool permission tags affected by the mode
    tool_permissions: Mapped[List[str]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
        comment="Tool permission tags for the allowlist/denylist",
    )

    # Interface-specific configuration (e.g. phone number, bot token)
    # Stored as plain JSONB — sensitive values should use org-level encryption.
    interface_config: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Interface credentials / settings (phone, bot_token, webhook_url, …)",
    )

    # UX preferences per interface (e.g. max message length, default format)
    preferences: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        comment="UX preferences: max_message_length, default_format, locale, …",
    )

    # ── Relationships ──────────────────────────────────────────────────
    organization: Mapped["GSageOrganization"] = relationship(
        back_populates="interface_profiles",
    )

    user: Mapped[Optional["GSageUser"]] = relationship(
        "GSageUser",
        foreign_keys=[user_id],
    )

    def __repr__(self) -> str:
        return (
            f"<GSageInterfaceProfile org={self.org_id} "
            f"interface={self.interface!r} mode={self.mode!r} "
            f"active={self.is_active}>"
        )

