"""gSage AI — Microsoft Teams rate limiter (Redis-backed).

Same pattern as ``src/telegram_worker/rate_limiter.py``:

  ratelimit:{org_id}:teams:{YYYY-MM-DD}        TTL 86 400 s
  ratelimit:{user_id}:teams_msgs:{YYYY-MM-DD-HH}  TTL  3 600 s
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def check_org_teams_rate(
    redis_client: Any,
    org_id: uuid.UUID,
    daily_limit: int = 200,
) -> bool:
    """Return True if the org is within its daily Teams message limit."""
    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    key = f"ratelimit:{org_id}:teams:{date_str}"

    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    results = await pipe.execute()
    count, ttl = results[0], results[1]

    if ttl < 0:
        await redis_client.expire(key, 86_400)

    if count > daily_limit:
        logger.warning(
            "check_org_teams_rate: ORG limit exceeded — org_id=%s count=%d limit=%d",
            org_id,
            count,
            daily_limit,
        )
        return False
    return True


async def check_user_teams_rate(
    redis_client: Any,
    user_id: uuid.UUID,
    hourly_limit: int = 30,
) -> bool:
    """Return True if the user is within their hourly Teams message limit."""
    hour_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d-%H")
    key = f"ratelimit:{user_id}:teams_msgs:{hour_str}"

    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    results = await pipe.execute()
    count, ttl = results[0], results[1]

    if ttl < 0:
        await redis_client.expire(key, 3_600)

    if count > hourly_limit:
        logger.warning(
            "check_user_teams_rate: USER limit exceeded — user_id=%s count=%d limit=%d",
            user_id,
            count,
            hourly_limit,
        )
        return False
    return True
