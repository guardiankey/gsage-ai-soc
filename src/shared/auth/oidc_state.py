"""gSage AI — Redis-backed state store for OIDC SSO flows.

Stores three kinds of short-lived secrets:

- ``oidc:state:{state}``       — Authorization request state (PKCE verifier,
                                 nonce, org_slug, provider, ``next``). TTL 10 min.
- ``oidc:session:{token}``     — One-shot session token issued after a
                                 successful callback. TTL 60 s, single-use.
- ``oidc:groups:{user_oid}:{org_id}`` — Cached Microsoft Graph group lookup
                                       result (used when ``_claim_names.groups``
                                       overage is signalled). TTL 5 min.

All entries are JSON-encoded.  Reads of the state and session tokens use a
delete-on-read (``GETDEL``) pattern to prevent replay.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_oidc_redis = None

DEFAULT_STATE_TTL = 600          # 10 minutes
DEFAULT_SESSION_TOKEN_TTL = 60   # 1 minute
DEFAULT_GROUPS_CACHE_TTL = 300   # 5 minutes


def get_oidc_redis_client():
    """Return a lazy-init async Redis client for OIDC state operations.

    Returns ``None`` if Redis is unavailable (callers must treat this as a
    fatal error for the SSO flow).
    """
    global _oidc_redis
    if _oidc_redis is not None:
        return _oidc_redis
    try:
        import redis.asyncio as aioredis  # type: ignore[import]

        from src.shared.config.settings import get_settings

        settings = get_settings()
        _oidc_redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
        )
        return _oidc_redis
    except Exception as exc:
        logger.error("OIDC state store: Redis client init failed — %s", exc)
        return None


# ---------------------------------------------------------------------------
# Authorization request state
# ---------------------------------------------------------------------------


async def save_state(
    state: str,
    payload: dict[str, Any],
    ttl: int = DEFAULT_STATE_TTL,
) -> bool:
    """Save the OIDC authorization-request state.

    Returns True on success, False if Redis is unavailable.
    """
    client = get_oidc_redis_client()
    if client is None:
        return False
    try:
        await client.set(f"oidc:state:{state}", json.dumps(payload), ex=ttl)
        return True
    except Exception as exc:
        logger.error("OIDC state store: save_state failed — %s", exc)
        return False


async def consume_state(state: str) -> Optional[dict[str, Any]]:
    """Read and atomically delete the OIDC state (one-shot).

    Returns the stored payload, or ``None`` when the state is unknown,
    expired, or already consumed.
    """
    client = get_oidc_redis_client()
    if client is None:
        return None
    key = f"oidc:state:{state}"
    try:
        # Prefer GETDEL (Redis 6.2+) for atomic read-and-delete; fall back
        # to a pipelined GET+DEL when GETDEL is unavailable.
        try:
            raw = await client.getdel(key)
        except AttributeError:
            pipe = client.pipeline(transaction=True)
            pipe.get(key)
            pipe.delete(key)
            raw, _ = await pipe.execute()
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.error("OIDC state store: consume_state failed — %s", exc)
        return None


# ---------------------------------------------------------------------------
# One-shot session tokens (handed to the browser at SSO completion)
# ---------------------------------------------------------------------------


async def save_session_token(
    token: str,
    payload: dict[str, Any],
    ttl: int = DEFAULT_SESSION_TOKEN_TTL,
) -> bool:
    client = get_oidc_redis_client()
    if client is None:
        return False
    try:
        await client.set(f"oidc:session:{token}", json.dumps(payload), ex=ttl)
        return True
    except Exception as exc:
        logger.error("OIDC state store: save_session_token failed — %s", exc)
        return False


async def consume_session_token(token: str) -> Optional[dict[str, Any]]:
    """Atomically read-and-delete a one-shot SSO session token."""
    client = get_oidc_redis_client()
    if client is None:
        return None
    key = f"oidc:session:{token}"
    try:
        try:
            raw = await client.getdel(key)
        except AttributeError:
            pipe = client.pipeline(transaction=True)
            pipe.get(key)
            pipe.delete(key)
            raw, _ = await pipe.execute()
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.error("OIDC state store: consume_session_token failed — %s", exc)
        return None


# ---------------------------------------------------------------------------
# Microsoft Graph group cache (for the >200-groups overage case)
# ---------------------------------------------------------------------------


async def get_cached_groups(user_oid: str, org_id: str) -> Optional[list[str]]:
    client = get_oidc_redis_client()
    if client is None:
        return None
    try:
        raw = await client.get(f"oidc:groups:{user_oid}:{org_id}")
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("OIDC state store: get_cached_groups failed — %s", exc)
        return None


async def set_cached_groups(
    user_oid: str,
    org_id: str,
    groups: list[str],
    ttl: int = DEFAULT_GROUPS_CACHE_TTL,
) -> bool:
    client = get_oidc_redis_client()
    if client is None:
        return False
    try:
        await client.set(
            f"oidc:groups:{user_oid}:{org_id}",
            json.dumps(groups),
            ex=ttl,
        )
        return True
    except Exception as exc:
        logger.warning("OIDC state store: set_cached_groups failed — %s", exc)
        return False
