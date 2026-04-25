"""gSage AI — Email rate limiter (Phase 7).

Implements Redis-backed rate limits per PROMPT.md Phase 7 spec:

  org_email_limit:   100 inbound emails per organization per calendar day.
  user_thread_limit: 10  new email threads per user per rolling hour.

Both limits use Redis INCR + EXPIRE (atomic via pipeline).

Return values:
  True  → within limit, request allowed.
  False → limit exceeded, request must be rejected.

Keys:
  ratelimit:{org_id}:email:{YYYY-MM-DD}      TTL 86 400 s (24 h)
  ratelimit:{user_id}:threads:{YYYY-MM-DD-HH}  TTL  3 600 s (1 h)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from src.shared.config.settings import get_settings

logger = logging.getLogger(__name__)


async def check_org_email_rate(
    redis_client: Any,
    org_id: uuid.UUID,
) -> bool:
    """Check whether the organization is within its daily email rate limit.

    Args:
        redis_client: An ``aioredis`` client instance (``redis.asyncio.Redis``).
        org_id:       Organization UUID.

    Returns:
        True if the org is within limit (increment applied).
        False if the limit has been reached (no increment applied).
    """
    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    key = f"ratelimit:{org_id}:email:{date_str}"

    # Use a pipeline for atomic INCR + conditional EXPIRE.
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    results = await pipe.execute()
    count, ttl = results[0], results[1]

    # Set TTL on first access (ttl == -1 means key exists but no expiry).
    if ttl < 0:
        await redis_client.expire(key, 86_400)

    limit = get_settings().email_rate_limit_org_daily
    if count > limit:
        logger.warning(
            "check_org_email_rate: ORG limit exceeded — org_id=%s count=%d limit=%d",
            org_id,
            count,
            limit,
        )
        return False

    logger.debug(
        "check_org_email_rate: allowed — org_id=%s count=%d/%d",
        org_id,
        count,
        limit,
    )
    return True


async def check_user_thread_rate(
    redis_client: Any,
    user_id: uuid.UUID,
) -> bool:
    """Check whether the user is within the new-thread hourly rate limit.

    This limit applies only when a *new* thread is created (not for replies
    to an existing thread).  The caller must only invoke this function when
    ``is_new_thread`` is True.

    Args:
        redis_client: An ``aioredis`` client instance.
        user_id:      User UUID.

    Returns:
        True if the user is within limit (increment applied).
        False if the limit has been reached (no increment applied).
    """
    # Key includes hour granularity (rolling per calendar hour UTC).
    hour_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d-%H")
    key = f"ratelimit:{user_id}:threads:{hour_str}"

    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    results = await pipe.execute()
    count, ttl = results[0], results[1]

    if ttl < 0:
        await redis_client.expire(key, 3_600)

    limit = get_settings().email_rate_limit_user_hourly
    if count > limit:
        logger.warning(
            "check_user_thread_rate: USER limit exceeded — user_id=%s count=%d limit=%d",
            user_id,
            count,
            limit,
        )
        return False

    logger.debug(
        "check_user_thread_rate: allowed — user_id=%s count=%d/%d",
        user_id,
        count,
        limit,
    )
    return True
