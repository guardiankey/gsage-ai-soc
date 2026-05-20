"""gSage AI — Tool Cache Decorator.

Provides @cached decorator for caching tool execution results with TTL and scope.

Usage examples:

    # Global cache (shared across all orgs) - for DNS, WHOIS, etc.
    @cached(ttl=3600, scope="global")
    async def whois_lookup(domain: str) -> dict:
        ...

    # Org-scoped cache (isolated per organization)
    @cached(ttl=1800, scope="org")
    async def get_org_statistics(org_id: uuid.UUID) -> dict:
        ...

    # Custom TTL in seconds
    @cached(ttl=300)  # 5 minutes
    async def quick_lookup(query: str) -> dict:
        ...

Non-serializable handling:
    - By default, raises TypeError for non-JSON-serializable args/results
    - Tool can pre-process args to ensure serializability
    - datetime objects are auto-converted to ISO strings
    - UUID objects are auto-converted to strings
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Optional, TypeVar

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.tool_cache import CacheScope, GSageToolCache

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ── JSON Encoder with datetime/UUID support ───────────────────────────────


class _CacheJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for cache serialization."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, uuid.UUID):
            return str(o)
        # Let the base class raise TypeError for other non-serializable types
        return super().default(o)


def _serialize_for_cache(value: Any) -> str:
    """
    Serialize value to JSON string.

    Raises:
        TypeError: If value contains non-serializable objects.
    """
    return json.dumps(value, cls=_CacheJSONEncoder, sort_keys=True)


def _deserialize_from_cache(value_str: str) -> Any:
    """Deserialize JSON string back to Python object."""
    return json.loads(value_str)


# ── Cache key generation ───────────────────────────────────────────────────


def _build_cache_key(
    tool_name: str,
    args: tuple,
    kwargs: dict,
    scope: CacheScope,
    org_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
) -> str:
    """
    Generate SHA256 cache key from function signature and scope.

    Format: sha256(tool_name + args + kwargs + scope + org_id + user_id)
    """
    components = {
        "tool": tool_name,
        "args": args,
        "kwargs": kwargs,
        "scope": scope.value,
    }
    if scope == CacheScope.ORG and org_id:
        components["org_id"] = str(org_id)
    elif scope == CacheScope.USER and user_id:
        components["user_id"] = str(user_id)

    serialized = _serialize_for_cache(components)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ── Decorator ──────────────────────────────────────────────────────────────


def cached(
    ttl: int = 3600,
    scope: Literal["global", "org", "user"] = "global",
    *,
    key_fn: Optional[Callable[..., str]] = None,
    ttl_fn: Optional[Callable[..., int]] = None,
    logical_name: Optional[str] = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Cache decorator for tool functions with TTL and scope isolation.

    Args:
        ttl: Default time-to-live in seconds (default: 3600 = 1 hour).
            Used as a fallback when ``ttl_fn`` is not provided or returns None.
        scope: Cache scope ("global", "org", or "user").
               - "global": Shared across all orgs (DNS, WHOIS, etc.)
               - "org": Isolated per organization (requires org_id in kwargs)
               - "user": Per-user isolation (requires user_id in kwargs)
        key_fn: Optional callable to derive a custom *logical* cache key string
            from the function's (args, kwargs). The decorator still SHA256-hashes
            the result together with the scope identifiers, so callers only need
            to provide a stable discriminator (e.g. ``"cisa_kev:feed:v1"`` or
            ``f"vt:{ioc}:{ioc_type}"``). When omitted, the default key builder
            uses all args/kwargs (minus ``session``).
        ttl_fn: Optional callable ``(result_value, *args, **kwargs) -> int`` that
            computes a per-call TTL in seconds. If it returns a positive int,
            that overrides ``ttl`` for the store operation. If it returns 0 or
            None, the result is NOT cached (useful for skipping error / empty
            results). If it raises, the call falls back to ``ttl``.
        logical_name: Optional identifier used in log messages and stored as
            ``tool_name`` on the cache row. Defaults to ``func.__name__``. Use
            this when the helper function name (e.g. ``_query_virustotal``)
            differs from the meaningful tool name (``threat_intel_lookup``).

    Function requirements:
        - Must accept a 'session' kwarg (AsyncSession) — cache is skipped otherwise.
        - For org scope: Must accept 'org_id' kwarg (uuid.UUID).
        - For user scope: Must accept 'user_id' kwarg (uuid.UUID).
        - All args/kwargs used for default key derivation must be JSON-serializable
          (or datetime/UUID). When ``key_fn`` is supplied this constraint only
          applies to the returned string.

    Concurrency:
        Insert uses PostgreSQL ``INSERT ... ON CONFLICT DO NOTHING`` so two
        concurrent callers racing on the same key will not create duplicate
        rows, nor raise UniqueViolation.

    Raises:
        ValueError: If required scope parameters (org_id/user_id) are missing.

    Example:
        @cached(ttl=1800, scope="org")
        async def expensive_query(org_id: uuid.UUID, query: str, session: AsyncSession):
            # ... expensive operation ...
            return {"result": "data"}

        @cached(
            ttl=7 * 24 * 3600,
            scope="global",
            key_fn=lambda *, ioc, ioc_type, **_: f"vt:{ioc_type}:{ioc}",
            logical_name="threat_intel_lookup",
        )
        async def _query_virustotal(*, ioc, ioc_type, session):
            ...
    """
    cache_scope = CacheScope(scope)

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        effective_name = logical_name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            # Extract session from kwargs (required for DB access)
            session: Optional[AsyncSession] = kwargs.get("session")
            if session is None:
                logger.warning(
                    "Cache decorator requires 'session' kwarg. Skipping cache for %s",
                    effective_name,
                )
                return await func(*args, **kwargs)

            # Extract scope identifiers
            org_id: Optional[uuid.UUID] = None
            user_id: Optional[uuid.UUID] = None

            if cache_scope == CacheScope.ORG:
                org_id = kwargs.get("org_id")
                if org_id is None:
                    raise ValueError(
                        f"Cache scope 'org' requires 'org_id' kwarg in {effective_name}"
                    )
            elif cache_scope == CacheScope.USER:
                user_id = kwargs.get("user_id")
                if user_id is None:
                    raise ValueError(
                        f"Cache scope 'user' requires 'user_id' kwarg in {effective_name}"
                    )

            # Build cache key — either via user-supplied key_fn (logical string)
            # or from the full call signature (default behaviour).
            try:
                if key_fn is not None:
                    logical_key = key_fn(*args, **kwargs)
                    components = {
                        "tool": effective_name,
                        "logical_key": str(logical_key),
                        "scope": cache_scope.value,
                    }
                    if cache_scope == CacheScope.ORG and org_id:
                        components["org_id"] = str(org_id)
                    elif cache_scope == CacheScope.USER and user_id:
                        components["user_id"] = str(user_id)
                    cache_key = hashlib.sha256(
                        _serialize_for_cache(components).encode("utf-8")
                    ).hexdigest()
                else:
                    cache_key = _build_cache_key(
                        tool_name=effective_name,
                        args=args,
                        kwargs={k: v for k, v in kwargs.items() if k != "session"},
                        scope=cache_scope,
                        org_id=org_id,
                        user_id=user_id,
                    )
            except TypeError as exc:
                logger.error(
                    "Failed to serialize args for %s: %s. Bypassing cache.",
                    effective_name,
                    exc,
                )
                return await func(*args, **kwargs)
            except Exception as exc:
                logger.error(
                    "key_fn raised for %s: %s. Bypassing cache.",
                    effective_name,
                    exc,
                )
                return await func(*args, **kwargs)

            # ── Check cache ────────────────────────────────────────────────
            # Use scalars().first() + order_by DESC to tolerate duplicate rows
            # (can appear if the unique constraint is not yet deployed or if
            # two legacy inserters raced). The newest entry wins.
            result = await session.execute(
                select(GSageToolCache)
                .where(GSageToolCache.cache_key == cache_key)
                .order_by(desc(GSageToolCache.created_at))
            )
            cache_entry = result.scalars().first()

            if cache_entry is not None:
                if cache_entry.is_expired():
                    try:
                        await session.delete(cache_entry)
                        await session.commit()
                    except Exception:
                        await session.rollback()
                    logger.debug(
                        "Cache expired for %s (key=%s)",
                        effective_name,
                        cache_key[:16],
                    )
                else:
                    # Cache hit!
                    cache_entry.increment_hit()
                    try:
                        await session.commit()
                    except Exception:
                        await session.rollback()
                    logger.debug(
                        "Cache HIT for %s (key=%s, hits=%d)",
                        effective_name,
                        cache_key[:16],
                        cache_entry.hit_count,
                    )
                    return cache_entry.value  # type: ignore[return-value]

            # ── Cache miss — execute function ──────────────────────────────
            logger.debug("Cache MISS for %s (key=%s)", effective_name, cache_key[:16])
            result_value = await func(*args, **kwargs)

            # ── Resolve effective TTL (ttl_fn can skip caching) ────────────
            effective_ttl: Optional[int] = ttl
            if ttl_fn is not None:
                try:
                    computed = ttl_fn(result_value, *args, **kwargs)
                    if computed is None or (isinstance(computed, int) and computed <= 0):
                        effective_ttl = None
                    else:
                        effective_ttl = int(computed)
                except Exception as exc:
                    logger.warning(
                        "ttl_fn raised for %s: %s. Using default ttl=%d.",
                        effective_name, exc, ttl,
                    )
                    effective_ttl = ttl

            if effective_ttl is None:
                return result_value

            # ── Store in cache (UPSERT-safe) ───────────────────────────────
            try:
                # Validate serializability up-front so we don't issue a doomed INSERT.
                _serialize_for_cache(result_value)

                stmt = (
                    pg_insert(GSageToolCache)
                    .values(
                        id=uuid.uuid4(),
                        scope=cache_scope,
                        org_id=org_id,
                        user_id=user_id,
                        tool_name=effective_name,
                        cache_key=cache_key,
                        value=result_value,
                        # expires_at is TIMESTAMP WITHOUT TIME ZONE — must be naive UTC
                        expires_at=datetime.utcnow() + timedelta(seconds=effective_ttl),
                        hit_count=0,
                    )
                    .on_conflict_do_nothing(index_elements=["cache_key"])
                )
                await session.execute(stmt)
                await session.commit()
                logger.debug(
                    "Cached result for %s (key=%s, ttl=%ds)",
                    effective_name,
                    cache_key[:16],
                    effective_ttl,
                )
            except TypeError as exc:
                logger.warning(
                    "Failed to cache result for %s: %s. Result not cached.",
                    effective_name,
                    exc,
                )
                await session.rollback()
            except Exception as exc:
                logger.exception(
                    "Unexpected error caching result for %s: %s",
                    effective_name,
                    exc,
                )
                # Don't fail the request, just skip caching
                await session.rollback()

            return result_value

        return wrapper

    return decorator
