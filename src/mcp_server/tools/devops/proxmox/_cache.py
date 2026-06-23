"""gSage AI — Lightweight Redis cache for the proxmox_* tool family.

Caches expensive read operations (cluster resource / node / storage
listings) so the same dashboard view rendered twice in 5 minutes hits
the Proxmox API once. Mutating actions (proxmox_manage) and single-object
``get_*`` lookups are never cached.

Key format::

    proxmox:{org_id}:{user_id}:{profile_id}:{pve_host}:{kind}:{sha256(filters)[:16]}

The key includes ``user_id`` to prevent cross-user payload leakage within
the same org, and ``pve_host`` so distinct profiles pointing at different
Proxmox clusters never collide.

TTL defaults to 300 seconds. Honour ``params.force_refresh=true`` by
skipping the cache lookup but still writing the fresh value.

Mirrors ``devops/vmware/_cache.py`` / ``devops/azure/_cache.py`` 1:1;
only the key prefix and the per-target field differ.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

import redis.asyncio as redis

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
_KEY_PREFIX = "proxmox"


def _hash_filters(filters: dict) -> str:
    payload = json.dumps(
        filters or {}, sort_keys=True, default=str, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_cache_key(
    *,
    org_id: str,
    user_id: str,
    profile_id: str,
    pve_host: str,
    kind: str,
    filters: Optional[dict] = None,
) -> str:
    return (
        f"{_KEY_PREFIX}:{org_id}:{user_id}:{profile_id}:"
        f"{pve_host}:{kind}:{_hash_filters(filters or {})}"
    )


class ProxmoxCache:
    """Minimal async Redis wrapper used by the proxmox_* tools."""

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
                log.debug("proxmox: error closing redis client", exc_info=True)
            self._client = None

    async def get(self, key: str) -> Optional[Any]:
        if self._client is None:
            return None
        try:
            raw = await self._client.get(key)
        except Exception as exc:
            log.warning("proxmox cache GET failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if self._client is None or ttl_seconds <= 0:
            return
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            await self._client.set(key, payload, ex=ttl_seconds)
        except Exception as exc:
            log.warning("proxmox cache SET failed: %s", exc)


async def get_cache() -> Optional[ProxmoxCache]:
    """Return a connected :class:`ProxmoxCache` using the global Settings.

    Returns ``None`` if Redis is unreachable so the caller can degrade
    gracefully (we never block the main operation on a cache failure).
    """
    try:
        from src.shared.config.settings import Settings  # noqa: PLC0415

        settings = Settings()  # type: ignore[call-arg]
        cache = ProxmoxCache(settings.redis_url)
        await cache.connect()
        return cache
    except Exception as exc:
        log.warning("proxmox: Redis cache unavailable: %s", exc)
        return None


async def cache_get(key: str, ttl: int = CACHE_TTL_SECONDS) -> Optional[Any]:
    """Convenience read; returns ``None`` on miss / failure."""
    if ttl <= 0:
        return None
    cache = await get_cache()
    if cache is None:
        return None
    try:
        return await cache.get(key)
    finally:
        await cache.close()


async def cache_set(
    key: str, value: Any, ttl: int = CACHE_TTL_SECONDS
) -> None:
    """Convenience write; silently no-ops on Redis failure."""
    if ttl <= 0:
        return
    cache = await get_cache()
    if cache is None:
        return
    try:
        await cache.set(key, value, ttl)
    finally:
        await cache.close()


__all__ = [
    "CACHE_TTL_SECONDS",
    "ProxmoxCache",
    "build_cache_key",
    "cache_get",
    "cache_set",
    "get_cache",
]
