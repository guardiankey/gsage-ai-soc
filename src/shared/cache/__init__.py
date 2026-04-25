"""gSage AI — Tool Cache Management.

Provides cache invalidation, statistics, and utility functions.

Usage:
    from src.shared.cache import invalidate_tool_cache, invalidate_org_cache, prune_expired_cache

    # Invalidate all cache entries for a specific tool
    await invalidate_tool_cache(session, tool_name="whois_lookup")

    # Invalidate all cache entries for an organization
    await invalidate_org_cache(session, org_id=some_uuid)

    # Prune all expired entries (run via Celery hourly)
    await prune_expired_cache(session)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.tool_cache import CacheScope, GSageToolCache

logger = logging.getLogger(__name__)

# Re-export decorator for convenience
from .decorator import cached  # noqa: E402, F401

__all__ = [
    "cached",
    "invalidate_tool_cache",
    "invalidate_org_cache",
    "invalidate_user_cache",
    "prune_expired_cache",
    "get_cache_stats",
]


# ── Cache invalidation ─────────────────────────────────────────────────────


async def invalidate_tool_cache(
    session: AsyncSession,
    tool_name: str,
    scope: Optional[CacheScope] = None,
) -> int:
    """
    Invalidate all cache entries for a specific tool.

    Args:
        session: Database session.
        tool_name: Name of the tool (e.g., "whois_lookup").
        scope: Optional scope filter (global, org, user).

    Returns:
        Number of cache entries deleted.
    """
    stmt = delete(GSageToolCache).where(GSageToolCache.tool_name == tool_name)

    if scope is not None:
        stmt = stmt.where(GSageToolCache.scope == scope)

    result = await session.execute(stmt)
    await session.commit()

    deleted_count = result.rowcount or 0  # type: ignore[attr-defined]
    logger.info(
        "Invalidated %d cache entries for tool=%s scope=%s",
        deleted_count,
        tool_name,
        scope.value if scope else "all",
    )
    return deleted_count


async def invalidate_org_cache(
    session: AsyncSession,
    org_id: uuid.UUID,
    tool_name: Optional[str] = None,
) -> int:
    """
    Invalidate all cache entries for a specific organization.

    Args:
        session: Database session.
        org_id: Organization UUID.
        tool_name: Optional tool filter (invalidate only specific tool).

    Returns:
        Number of cache entries deleted.
    """
    stmt = delete(GSageToolCache).where(
        GSageToolCache.org_id == org_id,
        GSageToolCache.scope == CacheScope.ORG,
    )

    if tool_name is not None:
        stmt = stmt.where(GSageToolCache.tool_name == tool_name)

    result = await session.execute(stmt)
    await session.commit()

    deleted_count = result.rowcount or 0  # type: ignore[attr-defined]
    logger.info(
        "Invalidated %d cache entries for org_id=%s tool=%s",
        deleted_count,
        org_id,
        tool_name or "all",
    )
    return deleted_count


async def invalidate_user_cache(
    session: AsyncSession,
    user_id: uuid.UUID,
    tool_name: Optional[str] = None,
) -> int:
    """
    Invalidate all cache entries for a specific user.

    Args:
        session: Database session.
        user_id: User UUID.
        tool_name: Optional tool filter (invalidate only specific tool).

    Returns:
        Number of cache entries deleted.
    """
    stmt = delete(GSageToolCache).where(
        GSageToolCache.user_id == user_id,
        GSageToolCache.scope == CacheScope.USER,
    )

    if tool_name is not None:
        stmt = stmt.where(GSageToolCache.tool_name == tool_name)

    result = await session.execute(stmt)
    await session.commit()

    deleted_count = result.rowcount or 0  # type: ignore[attr-defined]
    logger.info(
        "Invalidated %d cache entries for user_id=%s tool=%s",
        deleted_count,
        user_id,
        tool_name or "all",
    )
    return deleted_count


# ── Cache pruning ──────────────────────────────────────────────────────────


async def prune_expired_cache(session: AsyncSession) -> int:
    """
    Delete all expired cache entries (expires_at < now).

    This should be called periodically (hourly) via Celery beat scheduler.

    Args:
        session: Database session.

    Returns:
        Number of expired entries deleted.
    """
    now = datetime.now(timezone.utc)

    stmt = delete(GSageToolCache).where(GSageToolCache.expires_at < now)

    result = await session.execute(stmt)
    await session.commit()

    deleted_count = result.rowcount or 0  # type: ignore[attr-defined]
    logger.info("Pruned %d expired cache entries", deleted_count)
    return deleted_count


# ── Cache statistics ───────────────────────────────────────────────────────


async def get_cache_stats(session: AsyncSession) -> dict:
    """
    Get cache statistics (total entries, by scope, by tool, etc.).

    Returns:
        Dictionary with cache statistics:
        {
            "total_entries": 1234,
            "by_scope": {"global": 800, "org": 400, "user": 34},
            "by_tool": {"whois_lookup": 500, "dns_query": 300, ...},
            "expired_count": 50,
            "total_hits": 5000,
        }
    """
    now = datetime.now(timezone.utc)

    # Total entries
    total_result = await session.execute(select(func.count()).select_from(GSageToolCache))
    total = total_result.scalar_one() or 0

    # By scope
    scope_result = await session.execute(
        select(GSageToolCache.scope, func.count())
        .group_by(GSageToolCache.scope)
    )
    by_scope = {scope.value: count for scope, count in scope_result}

    # By tool (top 10)
    tool_result = await session.execute(
        select(GSageToolCache.tool_name, func.count())
        .group_by(GSageToolCache.tool_name)
        .order_by(func.count().desc())
        .limit(10)
    )
    by_tool = {tool: count for tool, count in tool_result}

    # Expired count
    expired_result = await session.execute(
        select(func.count())
        .select_from(GSageToolCache)
        .where(GSageToolCache.expires_at < now)
    )
    expired_count = expired_result.scalar_one() or 0

    # Total hits
    hits_result = await session.execute(
        select(func.sum(GSageToolCache.hit_count))
    )
    total_hits = hits_result.scalar_one() or 0

    return {
        "total_entries": total,
        "by_scope": by_scope,
        "by_tool": by_tool,
        "expired_count": expired_count,
        "total_hits": total_hits,
    }
