"""gSage AI — Tool Cache Model.

Persistent cache for tool execution results with TTL and multi-level scoping.

Scopes:
- GLOBAL: Shared across all orgs (e.g., whois, dns lookups)
- ORG: Isolated per organization
- USER: Per-user cache (future use)

Usage:
    - Cache is populated via @cached decorator in src/shared/cache/decorator.py
    - Pruning is handled by Celery task (hourly)
    - Invalidation can be done per tool or per org via cache utilities
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class CacheScope(str, Enum):
    """Scope level for cache isolation."""

    GLOBAL = "global"  # Shared across all orgs (DNS, WHOIS, etc.)
    ORG = "org"        # Per-organization cache
    USER = "user"      # Per-user cache (future)


class GSageToolCache(Base, TimestampMixin):
    """
    Tool execution cache with TTL and scope-based isolation.

    Cache key is a SHA256 hash of: tool_name + args + scope identifiers.
    """

    __tablename__ = "gsage_tool_cache"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Scope fields ───────────────────────────────────────────────────────
    scope: Mapped[CacheScope] = mapped_column(
        String(20),
        nullable=False,
        index=True,
        comment="Cache scope: global, org, or user",
    )

    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="Organization ID (NULL for global scope)",
    )

    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="User ID (NULL for global/org scope)",
    )

    # ── Cache key ──────────────────────────────────────────────────────────
    tool_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        index=True,
        comment="Fully-qualified tool name (e.g., 'whois_lookup', 'dns_query')",
    )

    cache_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="SHA256 hash of tool_name + args + scope identifiers",
    )

    # ── Cache value ────────────────────────────────────────────────────────
    value: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Cached result (JSON-serializable)",
    )

    # ── TTL ────────────────────────────────────────────────────────────────
    expires_at: Mapped[datetime] = mapped_column(
        nullable=False,
        index=True,
        comment="Cache expiration timestamp (UTC)",
    )

    # ── Metadata ───────────────────────────────────────────────────────────
    hit_count: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
        comment="Number of times this cache entry was hit",
    )

    last_hit_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True,
        comment="Last time this cache entry was accessed",
    )

    # ── Indexes ────────────────────────────────────────────────────────────
    __table_args__ = (
        # Composite index for efficient invalidation by tool
        Index("ix_tool_cache_tool_scope", "tool_name", "scope"),
        # Composite index for efficient invalidation by org
        Index("ix_tool_cache_org_scope", "org_id", "scope"),
        # Index for TTL-based pruning
        Index("ix_tool_cache_expires_at", "expires_at"),
    )

    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        # expires_at is stored as TIMESTAMP WITHOUT TIME ZONE (naive UTC)
        return datetime.utcnow() >= self.expires_at

    def increment_hit(self) -> None:
        """Increment hit count and update last_hit_at."""
        self.hit_count += 1
        # last_hit_at is stored as TIMESTAMP WITHOUT TIME ZONE (naive UTC)
        self.last_hit_at = datetime.utcnow()

    def __repr__(self) -> str:
        return (
            f"<GSageToolCache(tool={self.tool_name}, "
            f"scope={self.scope}, expires={self.expires_at})>"
        )
