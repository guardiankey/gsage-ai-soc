"""gSage AI — Redis invalidation for the per-org tool-config cache.

The MCP server caches decrypted, merged tool configs in Redis under the key
pattern ``toolcfg:{org_id}:{tool_name}:{profile_id}`` (TTL 5 min, written by
``src.mcp_server.tools.base.BaseTool.load_config``).  Because that cache lives
in a different process from the Backend API, admin edits to a tool config must
explicitly invalidate it — otherwise the MCP server keeps serving the stale
config until the TTL expires.

Two subtleties make a *broad* org-wide flush the safe default:

1. ``load_config`` keys the cache by ``self.name`` (the concrete tool), but a
   config may be stored under a shared ``config_namespace`` (e.g. ``sei_pen``
   feeding both ``sei_pen_read`` and ``sei_pen_write``).  Invalidating only the
   edited ``tool_name`` would leave the per-tool caches stale.
2. Profiles add a third key segment, so a single tool can have several entries.

Flushing ``toolcfg:{org_id}:*`` on any config change covers all of these and is
cheap, since tool-config edits are rare admin operations.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

# Must match ``src.mcp_server.tools.base.TOOL_CONFIG_CACHE_KEY``.
_TOOL_CONFIG_KEY_PREFIX = "toolcfg"


async def invalidate_tool_config_cache(
    redis_client,
    org_id: uuid.UUID,
) -> int:
    """Delete every cached tool config for an organization.

    Returns the number of Redis keys removed.  Errors are logged and swallowed
    (the 5-minute TTL acts as a safety net), so callers never fail an admin
    request because of a cache hiccup.
    """
    if redis_client is None:
        return 0
    pattern = f"{_TOOL_CONFIG_KEY_PREFIX}:{org_id}:*"
    try:
        keys = [k async for k in redis_client.scan_iter(match=pattern)]
        if not keys:
            return 0
        count = await redis_client.delete(*keys)
        logger.debug(
            "tool config cache: invalidated %d key(s) pattern=%s", count, pattern
        )
        return count
    except Exception as exc:  # pragma: no cover — best-effort invalidation
        logger.warning(
            "tool config cache invalidation error pattern=%s: %s", pattern, exc
        )
        return 0
