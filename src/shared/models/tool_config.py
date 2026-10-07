"""gSage AI — Tool configuration models (per-org).

A tool configuration (:class:`GSageToolConfig`) holds an AES-256-GCM encrypted
JSON payload (API keys, endpoints, thresholds, …) and has an explicit
:attr:`~GSageToolConfig.scope`:

* ``org``  → **global**, applies to the whole organization;
* ``dept`` → applies **only** to the departments listed in the
  :class:`GSageToolConfigDepartment` join table.

For one ``(org, tool_name, profile_id)`` there may be **one global config plus
one or more department-scoped configs** whose department sets are disjoint
(enforced by the join-table unique key). At resolution time the department
config is **merged over** the global one (dept keys win).

Multiple profiles per (org, tool) are still supported when the tool declares
``supports_multiple_configs = True`` — each profile has its own row identified
by ``profile_id`` (single-config tools always use ``profile_id = 'default'``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.shared.security.encryption import get_encryption

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization


class GSageToolConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-organization tool configuration.

    Stores encrypted JSONB config (API keys, endpoints, thresholds, etc.).
    :attr:`scope` is ``'org'`` (global) or ``'dept'``; department-scoped configs
    list their departments through :attr:`departments`.  Multiple profiles per
    (org, tool) are supported when the tool declares
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

    # Scope — 'org' = global (all departments); 'dept' = listed departments only
    scope: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="org",
        server_default="org",
        comment="'org' = org-wide (all departments); 'dept' = the departments in "
                "gsage_tool_config_departments. At most one 'org' row per "
                "(org_id, tool_name, profile_id).",
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
    departments: Mapped[list["GSageToolConfigDepartment"]] = relationship(
        "GSageToolConfigDepartment",
        back_populates="tool_config",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="GSageToolConfigDepartment.dept_id",
    )

    __table_args__ = (
        # At most one *global* config per (org, tool, profile). Department-scoped
        # configs are unbounded here; the join-table unique key keeps their
        # department sets disjoint.
        Index(
            "uq_tool_configs_global",
            "org_id", "tool_name", "profile_id",
            unique=True,
            postgresql_where=text("scope = 'org'"),
        ),
        CheckConstraint("scope IN ('org', 'dept')", name="ck_tool_configs_scope"),
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


class GSageToolConfigDepartment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Join table: which departments a ``scope='dept'`` tool config applies to.

    Populated only for department-scoped configs (a global config,
    ``scope='org'``, has no rows here).  N rows mean the config applies only to
    those N departments.

    ``org_id`` / ``tool_name`` / ``profile_id`` are denormalized from
    :class:`GSageToolConfig` so the unique constraint below can enforce the
    *overlap rule* — the same department cannot be attached to two different
    configs of the same ``(org, tool_name, profile_id)``.
    """

    __tablename__ = "gsage_tool_config_departments"

    tool_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_tool_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized keys (kept in sync with the parent config).
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    profile_id: Mapped[str] = mapped_column(
        String(100), nullable=False, default="default", server_default="default"
    )
    dept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    tool_config: Mapped[GSageToolConfig] = relationship(
        "GSageToolConfig", back_populates="departments"
    )

    __table_args__ = (
        UniqueConstraint(
            "org_id", "tool_name", "profile_id", "dept_id",
            name="uq_tool_config_dept_scope",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageToolConfigDepartment(config_id={self.tool_config_id}, "
            f"dept_id={self.dept_id})>"
        )
