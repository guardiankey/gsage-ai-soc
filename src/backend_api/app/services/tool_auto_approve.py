"""HITL auto-approval lookup.

Resolves whether HITL approvals for a given (org, tool) should be
auto-approved by the backend. Follows the project's standard config
precedence:

1. ``GSageToolConfig.config["auto_approve"]`` (per-org, ``profile_id="default"``).
2. Environment variable ``TOOL_<TOOL_NAME_UPPER>__AUTO_APPROVE``.
3. ``False`` (no auto-approval).

A short in-memory TTL cache (30s) avoids hammering the DB at every
``run_paused`` event. The cache is intentionally small/simple — for
multi-replica deployments each replica computes its own view; a 30s
window of staleness after a config change is acceptable.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

from sqlalchemy import select

from src.shared.database import _get_session_maker
from src.shared.models.tool_config import GSageToolConfig

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30
# Cache entries: (org_id, tool_name) -> (value, expires_at_monotonic)
_cache: dict[tuple[uuid.UUID, str], tuple[bool, float]] = {}

_TRUTHY = {"1", "true", "yes", "on"}


def _coerce_bool(value: object) -> Optional[bool]:
    """Coerce a config value to bool. Returns None when value is missing/None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return None


def _env_auto_approve(tool_name: str) -> Optional[bool]:
    """Read ``TOOL_<NAME_UPPER>__AUTO_APPROVE`` from the environment."""
    env_key = f"TOOL_{tool_name.upper()}__AUTO_APPROVE"
    raw = os.environ.get(env_key)
    if raw is None:
        return None
    return _coerce_bool(raw)


async def _db_auto_approve(
    *, org_id: uuid.UUID, tool_name: str
) -> Optional[bool]:
    """Read ``auto_approve`` from the ``GSageToolConfig`` row (profile=default).

    Opens a **fresh** ``AsyncSession`` so this lookup is safe to call from
    contexts where the request-scoped session is already closed (e.g. the
    SSE generator that keeps streaming after FastAPI finalised the
    response). Returns ``None`` when there is no row, or when the row
    exists but does not declare ``auto_approve``. Decryption errors are
    logged and treated as ``None`` so the caller falls back to env/default.
    """
    stmt = select(GSageToolConfig).where(
        GSageToolConfig.org_id == org_id,
        GSageToolConfig.tool_name == tool_name,
        GSageToolConfig.profile_id == "default",
    )
    session_maker = _get_session_maker()
    async with session_maker() as session:
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
    if row is None:
        return None
    try:
        config = row.config
    except Exception as exc:
        log.warning(
            "auto_approve: failed to decrypt config for org=%s tool=%s: %s",
            org_id, tool_name, exc,
        )
        return None
    if not isinstance(config, dict):
        return None
    if "auto_approve" not in config:
        return None
    return _coerce_bool(config["auto_approve"])


async def is_auto_approve(
    *, org_id: uuid.UUID, tool_name: str
) -> bool:
    """Return True when HITL approvals for this tool should be auto-approved.

    Precedence: DB toolconfig > env var > ``False``.
    """
    now = time.monotonic()
    key = (org_id, tool_name)
    cached = _cache.get(key)
    if cached is not None and cached[1] > now:
        log.debug(
            "is_auto_approve: cache HIT org=%s tool=%s → %s",
            org_id, tool_name, cached[0],
        )
        return cached[0]

    db_value = await _db_auto_approve(org_id=org_id, tool_name=tool_name)
    if db_value is not None:
        resolved = db_value
        log.debug(
            "is_auto_approve: DB resolved org=%s tool=%s → %s",
            org_id, tool_name, resolved,
        )
    else:
        env_value = _env_auto_approve(tool_name)
        resolved = env_value if env_value is not None else False
        log.debug(
            "is_auto_approve: ENV resolved org=%s tool=%s env=%s → %s",
            org_id, tool_name, env_value, resolved,
        )

    _cache[key] = (resolved, now + _CACHE_TTL_SECONDS)
    return resolved


def invalidate_cache(
    *, org_id: Optional[uuid.UUID] = None, tool_name: Optional[str] = None
) -> None:
    """Invalidate cached auto_approve values.

    Called after a ``GSageToolConfig`` row changes via the admin API.
    With no args, clears the entire cache.
    """
    if org_id is None and tool_name is None:
        _cache.clear()
        return
    for key in list(_cache.keys()):
        if org_id is not None and key[0] != org_id:
            continue
        if tool_name is not None and key[1] != tool_name:
            continue
        _cache.pop(key, None)
