"""gSage AI — Tool configuration model (per-org)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
import uuid

from sqlalchemy import ForeignKey, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.shared.security.encryption import get_encryption

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization


class GSageToolConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-organization tool configuration.

    Stores encrypted JSONB config (API keys, endpoints, thresholds, etc.).
    Multiple profiles per (org, tool) are supported when the tool declares
    ``supports_multiple_configs = True`` — each profile has its own row
    identified by ``profile_id``.  Single-config tools always use
    ``profile_id = 'default'``.
    """

    __tablename__ = "gsage_tool_configs"

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
        comment="NULL = org-wide config; set = department-specific override.",
    )

    # Tool identification
    tool_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Matches tool registry name (e.g., dns_lookup)",
    )

    # Config profile — supports multiple instances of the same tool per org
    profile_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="default",
        server_default="default",
        comment="Profile identifier (e.g. 'vt_free', 'misp_prod'). "
                "'default' for single-config tools.",
    )

    # Human-readable label shown in UI and injected into agent description
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable label for this profile "
                "(e.g. 'VirusTotal free tier — 4 req/min')",
    )

    # Encrypted configuration (JSONB encrypted with AES-256-GCM)
    _config_encrypted: Mapped[bytes] = mapped_column(
        "config_encrypted",
        LargeBinary,
        nullable=False,
        comment="AES-256-GCM encrypted JSONB payload",
    )

    # Audit
    updated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Admin who last modified this config",
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship("GSageOrganization")

    __table_args__ = (
        UniqueConstraint(
            "org_id", "dept_id", "tool_name", "profile_id",
            name="uq_tool_configs_org_dept_tool_profile",
        ),
    )

    @property
    def config(self) -> dict:
        """Decrypt and return config as dict."""
        import json
        decrypted_json = get_encryption().decrypt(self._config_encrypted)
        return json.loads(decrypted_json) if decrypted_json else {}

    @config.setter
    def config(self, value: dict) -> None:
        """Encrypt and store config dict."""
        import json
        json_str = json.dumps(value)
        self._config_encrypted = get_encryption().encrypt(json_str)

    def __repr__(self) -> str:
        return (
            f"<GSageToolConfig(id={self.id}, org_id={self.org_id}, "
            f"tool_name={self.tool_name}, profile_id={self.profile_id})>"
        )
