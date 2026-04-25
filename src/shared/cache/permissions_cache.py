"""gSage AI — Shared Redis cache helpers for tool permission resolution.

Cache key format:
    toolperms:{org_id}:{user_id}:{interface}:{dept_id}

where ``dept_id`` is either a UUID string or ``none`` (when dept_id is None).

Invalidation helpers are imported by:
- src/mcp_server/permissions.py  — read/write cache
- src/backend_api/app/api/v1/admin_groups.py    — invalidate on group member/perm changes
- src/backend_api/app/api/v1/admin_interfaces.py — invalidate on interface profile changes
- admin_console/services/group_service.py        — invalidate on group changes (TUI)
- admin_console/services/tool_service.py         — invalidate on interface profile changes (TUI)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# TTL for each cached permission entry (seconds).
# Short TTL acts as safety net even if an explicit invalidation is missed.
PERM_CACHE_TTL = 30

# Module-level lazy singleton — one per process (Backend API, Admin Console).
# The MCP server injects _state.redis_client directly instead.
_perm_redis = None


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


def _perm_cache_key(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    interface: str,
    dept_id: Optional[uuid.UUID],
) -> str:
    dept = str(dept_id) if dept_id is not None else "none"
    return f"toolperms:{org_id}:{user_id}:{interface}:{dept}"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


async def get_cached_permissions(
    redis_client,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    interface: str,
    dept_id: Optional[uuid.UUID],
) -> Optional[list[str]]:
    """Return cached permission tags, or None on miss / error."""
    key = _perm_cache_key(org_id, user_id, interface, dept_id)
    try:
        value = await redis_client.get(key)
        if value is not None:
            return json.loads(value)
    except Exception as exc:
        logger.warning("permissions cache get error key=%s: %s", key, exc)
    return None


async def set_cached_permissions(
    redis_client,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    interface: str,
    dept_id: Optional[uuid.UUID],
    tags: list[str],
) -> None:
    """Store permission tags in Redis with PERM_CACHE_TTL."""
    key = _perm_cache_key(org_id, user_id, interface, dept_id)
    try:
        await redis_client.setex(key, PERM_CACHE_TTL, json.dumps(tags))
    except Exception as exc:
        logger.warning("permissions cache set error key=%s: %s", key, exc)


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


async def invalidate_user_permissions(
    redis_client,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Delete all cached permission keys for one user (all interfaces/depts)."""
    return await _delete_by_pattern(redis_client, f"toolperms:{org_id}:{user_id}:*")


async def invalidate_org_permissions(
    redis_client,
    org_id: uuid.UUID,
) -> int:
    """Delete all cached permission keys for an entire org."""
    return await _delete_by_pattern(redis_client, f"toolperms:{org_id}:*")


async def _delete_by_pattern(redis_client, pattern: str) -> int:
    try:
        keys = [k async for k in redis_client.scan_iter(match=pattern)]
        if keys:
            count = await redis_client.delete(*keys)
            logger.debug(
                "permissions cache: invalidated %d key(s) pattern=%s", count, pattern
            )
            return count
    except Exception as exc:
        logger.warning(
            "permissions cache invalidation error pattern=%s: %s", pattern, exc
        )
    return 0


# ---------------------------------------------------------------------------
# Lazy Redis client (for Backend API and Admin Console callers)
# ---------------------------------------------------------------------------


def get_perm_redis_client():
    """Return a lazy-init async Redis client for permission cache operations.

    Returns None if Redis is unavailable (so callers can skip invalidation
    gracefully — the TTL will expire stale entries).
    """
    global _perm_redis
    if _perm_redis is not None:
        return _perm_redis
    try:
        import redis.asyncio as aioredis  # type: ignore[import]

        from src.shared.config.settings import get_settings

        settings = get_settings()
        _perm_redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
        )
        return _perm_redis
    except Exception as exc:
        logger.warning("permissions cache: Redis client init failed — %s", exc)
        return None
