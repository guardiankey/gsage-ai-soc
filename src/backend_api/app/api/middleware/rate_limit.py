"""gSage AI — Redis-based rate limiting (Sprint 5.2).

Strategy
--------
Two sliding-window counters per request (INCR + EXPIRE in a Redis pipeline):

1. **Org-level**  ``rl:org:{org_id}``           — total RPM across the org
   Default: ``settings.rate_limit_default_rpm``  (overridable per API key)

2. **User-level** ``rl:user:{org_id}:{user_id}`` — RPM for a single user
   Default: ``settings.rate_limit_user_rpm``

The :func:`check_rate_limit` function is a FastAPI dependency that is
registered on the org-scoped ``APIRouter`` in ``router.py`` via::

    org_router = APIRouter(dependencies=[Depends(check_rate_limit)])

It declares ``TenantContext = Depends(get_tenant_context)`` as a
sub-dependency, guaranteeing that ``get_tenant_context`` always runs first
(FastAPI resolves the dependency DAG in topological order and caches results
within a single request, so no duplicate DB queries occur).

Response headers on every checked request
------------------------------------------
``X-RateLimit-Limit``      applicable limit (org or user, whichever is lower)
``X-RateLimit-Remaining``  remaining requests in the current window
``X-RateLimit-Reset``      UTC epoch-second when the window resets

On violation: HTTP 429 with ``Retry-After`` header.
"""

from __future__ import annotations

import time
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.shared.config.settings import get_settings

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis connection (lazy singleton per process — fail open if unavailable)
# ---------------------------------------------------------------------------

_redis: Optional[aioredis.Redis] = None  # type: ignore[type-arg]
_WINDOW = 60  # sliding window in seconds


def _get_redis() -> Optional[aioredis.Redis]:  # type: ignore[type-arg]
    global _redis
    if _redis is not None:
        return _redis
    try:
        settings = get_settings()
        _redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=1,
        )
        return _redis
    except Exception:
        log.warning("Rate limiter: Redis unavailable — rate limiting disabled")
        return None


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def check_rate_limit(
    request: Request,
    tc: TenantContext = Depends(get_tenant_context),
) -> None:
    """Enforce org + user rate limits for tenant-authenticated requests.

    Raises ``HTTP 429`` when a limit is exceeded.  Sets
    ``request.state.rl_{limit,remaining,reset}`` so that
    :class:`RateLimitHeadersMiddleware` can inject the headers.
    """
    settings = get_settings()

    if not settings.rate_limit_enabled:
        return

    redis_conn = _get_redis()
    if redis_conn is None:
        return  # fail open

    org_id = str(tc.org_id)
    user_id = str(tc.user_id)

    org_limit = tc.rate_limit_per_minute or settings.rate_limit_default_rpm
    user_limit = settings.rate_limit_user_rpm

    now = int(time.time())
    org_key = f"rl:org:{org_id}"
    user_key = f"rl:user:{org_id}:{user_id}"

    try:
        pipe = redis_conn.pipeline(transaction=False)
        pipe.incr(org_key)
        pipe.incr(user_key)
        org_count, user_count = await pipe.execute()
        # Set TTL only when the key is first created (fixed window).
        # Calling EXPIRE on every request would keep resetting the window,
        # causing the counter to accumulate indefinitely for active users.
        if org_count == 1:
            await redis_conn.expire(org_key, _WINDOW)
        if user_count == 1:
            await redis_conn.expire(user_key, _WINDOW)
    except Exception:
        log.debug("Rate limiter: pipeline error — failing open", exc_info=True)
        return

    org_count = int(org_count)
    user_count = int(user_count)
    reset = now + _WINDOW

    # Store for header injection (RateLimitHeadersMiddleware reads these)
    request.state.rl_limit = org_limit
    request.state.rl_remaining = max(0, org_limit - org_count)
    request.state.rl_reset = reset

    if org_count > org_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Organization rate limit exceeded ({org_limit} req/min)",
            headers={
                "X-RateLimit-Limit": str(org_limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset),
                "Retry-After": str(max(1, reset - now)),
            },
        )

    if user_count > user_limit:
        raise HTTPException(
            status_code=429,
            detail=f"User rate limit exceeded ({user_limit} req/min)",
            headers={
                "X-RateLimit-Limit": str(user_limit),
                "X-RateLimit-Remaining": str(max(0, user_limit - user_count)),
                "X-RateLimit-Reset": str(reset),
                "Retry-After": str(max(1, reset - now)),
            },
        )


# ---------------------------------------------------------------------------
# Lightweight ASGI middleware — injects X-RateLimit-* headers on responses
# ---------------------------------------------------------------------------


class RateLimitHeadersMiddleware:
    """Pure-ASGI middleware that appends rate-limit response headers.

    The headers are sourced from ``request.state.rl_*`` which is populated by
    the :func:`check_rate_limit` FastAPI dependency.  Only adds headers when
    the dependency ran (i.e. for org-authenticated routes).

    Register in ``main.py``::

        app.add_middleware(RateLimitHeadersMiddleware)
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        async def _send(message):
            if message["type"] == "http.response.start":
                rl_limit = getattr(request.state, "rl_limit", None)
                if rl_limit is not None:
                    extra = [
                        (b"x-ratelimit-limit", str(rl_limit).encode()),
                        (b"x-ratelimit-remaining", str(request.state.rl_remaining).encode()),
                        (b"x-ratelimit-reset", str(request.state.rl_reset).encode()),
                    ]
                    message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, _send)
