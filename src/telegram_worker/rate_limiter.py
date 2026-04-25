"""gSage AI — Telegram rate limiter.

Redis-backed rate limits for the Telegram channel:

  org_telegram_limit:  configurable inbound messages per organization per calendar day.
  user_telegram_limit: configurable messages per user per rolling hour.

Both limits use Redis INCR + EXPIRE (atomic via pipeline).

Return values:
  True  → within limit, request allowed.
  False → limit exceeded, request must be rejected.

Keys:
  ratelimit:{org_id}:telegram:{YYYY-MM-DD}      TTL 86 400 s (24 h)
  ratelimit:{user_id}:tg_msgs:{YYYY-MM-DD-HH}   TTL  3 600 s (1 h)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def check_org_telegram_rate(
    redis_client: Any,
    org_id: uuid.UUID,
    daily_limit: int = 200,
) -> bool:
    """Check whether the organization is within its daily Telegram rate limit.

    Args:
        redis_client: An ``aioredis`` client instance (``redis.asyncio.Redis``).
        org_id:       Organization UUID.
        daily_limit:  Maximum messages per calendar day (UTC). Default 200.

    Returns:
        True if within limit (increment applied).
        False if limit exceeded (no increment applied).
    """
    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    key = f"ratelimit:{org_id}:telegram:{date_str}"

    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    results = await pipe.execute()
    count, ttl = results[0], results[1]

    if ttl < 0:
        await redis_client.expire(key, 86_400)

    if count > daily_limit:
        logger.warning(
            "check_org_telegram_rate: ORG limit exceeded — org_id=%s count=%d limit=%d",
            org_id,
            count,
            daily_limit,
        )
        return False

    logger.debug(
        "check_org_telegram_rate: allowed — org_id=%s count=%d/%d",
        org_id,
        count,
        daily_limit,
    )
    return True


async def check_user_telegram_rate(
    redis_client: Any,
    user_id: uuid.UUID,
    hourly_limit: int = 30,
) -> bool:
    """Check whether the user is within the hourly Telegram message rate limit.

    Args:
        redis_client: An ``aioredis`` client instance.
        user_id:      User UUID.
        hourly_limit: Maximum messages per calendar hour (UTC). Default 30.

    Returns:
        True if within limit (increment applied).
        False if limit exceeded (no increment applied).
    """
    hour_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d-%H")
    key = f"ratelimit:{user_id}:tg_msgs:{hour_str}"

    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    results = await pipe.execute()
    count, ttl = results[0], results[1]

    if ttl < 0:
        await redis_client.expire(key, 3_600)

    if count > hourly_limit:
        logger.warning(
            "check_user_telegram_rate: USER limit exceeded — user_id=%s count=%d limit=%d",
            user_id,
            count,
            hourly_limit,
        )
        return False

    logger.debug(
        "check_user_telegram_rate: allowed — user_id=%s count=%d/%d",
        user_id,
        count,
        hourly_limit,
    )
    return True
