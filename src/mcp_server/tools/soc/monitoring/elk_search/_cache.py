"""gSage AI — Lightweight Redis cache for elk_search metadata queries.

Caches only **cheap, idempotent metadata operations** (``list_indices``
and ``describe_index``); actual ``search`` results are never cached.

Key format::

    elk_search:{org_id}:{profile_id}:{mode}:{sha256(params)}

TTL: per-profile ``cache_ttl_seconds`` (default 60).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

import redis.asyncio as redis

log = logging.getLogger(__name__)

_KEY_PREFIX = "elk_search"


def _hash_params(params: dict) -> str:
    payload = json.dumps(params, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_cache_key(
    *,
    org_id: str,
    profile_id: str,
    mode: str,
    params: dict,
) -> str:
    return f"{_KEY_PREFIX}:{org_id}:{profile_id}:{mode}:{_hash_params(params)}"


class ElkCache:
    """Minimal async Redis wrapper used by elk_search."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._client: Optional[redis.Redis] = None

    async def connect(self) -> None:
        if self._client is None:
            self._client = redis.from_url(self._url, decode_responses=True)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                log.debug("elk_search: error closing redis client", exc_info=True)
            self._client = None

    async def get(self, key: str) -> Optional[dict]:
        if self._client is None:
            return None
        try:
            raw = await self._client.get(key)
        except Exception as exc:
            log.warning("elk_search cache GET failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    async def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        if self._client is None or ttl_seconds <= 0:
            return
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            await self._client.set(key, payload, ex=ttl_seconds)
        except Exception as exc:
            log.warning("elk_search cache SET failed: %s", exc)


async def get_cache() -> Optional[ElkCache]:
    """Return a connected :class:`ElkCache` using the global Settings.

    Returns ``None`` if Redis is unreachable so the caller can degrade
    gracefully (we never block the main operation on a cache failure).
    """
    try:
        from src.shared.config.settings import Settings  # noqa: PLC0415

        settings = Settings()  # type: ignore[call-arg]
        cache = ElkCache(settings.redis_url)
        await cache.connect()
        return cache
    except Exception as exc:
        log.warning("elk_search: Redis cache unavailable: %s", exc)
        return None
