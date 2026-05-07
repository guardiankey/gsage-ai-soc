"""gSage AI — Tool registry models."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GSageTool(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Tool registry model.

    Stores metadata about available tools, their schemas, versions, and permissions.
    This is the source of truth for tool discovery and filtering.
    """

    __tablename__ = "gsage_tools"

    # Tool identification
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Tool name (e.g., dns_lookup)",
    )
    version: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Semantic version (e.g., 1.0.0)",
    )

    # Metadata
    display_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Human-readable name",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Description for LLM and UI",
    )
    summary: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment="One-line tool summary used in tool search results and LLM context",
    )
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Category: dns, network, decode, threat_intel, etc.",
    )

    # Permissions required to execute
    required_permissions: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of permission tags required (e.g., ['dns:read'])",
    )

    # Schema definitions (JSON Schema)
    input_schema: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="JSON Schema for input parameters",
    )
    output_schema: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="JSON Schema for output (canonical format)",
    )

    # Configuration and state schemas (optional)
    config_schema: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="JSON Schema for per-org configuration (tool_configs table)",
    )
    config_defaults: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Default config values when no org config exists",
    )
    state_schema: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="JSON Schema for per-org runtime state (tool_state table)",
    )
    state_defaults: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Default state values (initial + after reset)",
    )
    reset_policy: Mapped[str] = mapped_column(
        String(20),
        default="never",
        nullable=False,
        comment="State reset policy: daily, monthly, never",
    )

    # Execution settings
    timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        default=10,
        nullable=False,
        comment="Individual tool timeout",
    )
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer,
        default=60,
        nullable=False,
        comment="Default rate limit per org (can be overridden via tool_state)",
    )
    requires_config: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
        comment="If true, tool fails without org config",
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        default=True,
        nullable=False,
        comment="Admin can disable tool globally",
    )

    def __repr__(self) -> str:
        return f"<GSageTool(id={self.id}, name={self.name}, version={self.version})>"
