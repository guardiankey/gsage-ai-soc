"""gSage AI — E-goi tag resolver (id ↔ name).

The E-goi API references tags by integer ``tag_id`` everywhere — both in
``GET /lists/{id}/contacts`` payloads and in ``attach_tag``/``detach_tag``
bodies. Humans (and LLM agents) think in tag *names*, so this module
provides a small bidirectional cache:

* ``get_tag_index(client)`` — paginates ``client.get_all_tags()`` once
  and caches the result for ``_TTL_SECONDS`` per E-goi account.
* ``resolve_tag_value(value, *, index)`` — accepts ``int`` (validated
  against the index) or ``str`` (case-insensitive name lookup) and
  returns the canonical ``int`` ``tag_id``. Raises :class:`ValueError`
  with a fuzzy hint (`difflib.get_close_matches`) when the name is
  unknown.
* ``resolve_tags(values, *, client)`` — convenience wrapper that fetches
  the index once and surfaces *all* invalid inputs in a single error.
* ``invalidate(client)`` — drops the cache entry; intended hook for
  future ``create_tag``/``update_tag`` actions.

The cache is process-local. Thundering-herd is prevented with one
:class:`asyncio.Lock` per cache key (api_key hash). Memory footprint
is negligible (a few hundred small strings per account).
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from src.mcp_server.tools.marketing.egoi._client import EgoiClient

log = logging.getLogger(__name__)


# Refresh cached entries after this many seconds. Tags rarely change
# during a single tool run; a 5-minute TTL keeps the system responsive
# to ad-hoc edits in the E-goi UI without hammering ``/tags``.
_TTL_SECONDS = 300

# Page size used when paginating ``get_all_tags`` (the endpoint caps at
# the standard E-goi ``limit`` ceiling of 1000).
_TAG_PAGE_SIZE = 1000

# Number of suggestions surfaced when a tag name is not found.
_FUZZY_SUGGESTIONS = 3


@dataclass
class TagIndex:
    """Bidirectional tag mapping for one E-goi account."""

    by_id: dict[int, str] = field(default_factory=dict)
    by_name_lc: dict[str, int] = field(default_factory=dict)
    expires_at: float = 0.0

    def is_fresh(self, *, now: Optional[float] = None) -> bool:
        return (now or time.monotonic()) < self.expires_at

    def names(self) -> list[str]:
        """Return all known tag names (original case) for fuzzy hints."""
        return list(self.by_id.values())


# Module-level cache keyed by ``sha256(api_key)[:16]`` so we don't keep
# the raw key in memory beyond the EgoiClient instance.
_CACHE: dict[str, TagIndex] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _cache_key(client: "EgoiClient") -> str:
    api_key = getattr(client, "_api_key", "") or ""
    if not api_key:
        # Fall back to the object id so anonymous/test clients still get
        # an isolated cache entry rather than colliding with each other.
        return f"anon:{id(client):x}"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _get_lock(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _extract_tags_from_payload(payload: Any) -> list[dict]:
    """Pull the ``items`` list out of a ``get_all_tags`` payload."""
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [t for t in items if isinstance(t, dict)]
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    return []


def _total_items(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        total = payload.get("total_items")
        if isinstance(total, int):
            return total
    return None


async def _load_tag_index(client: "EgoiClient") -> TagIndex:
    """Paginate ``/tags`` and build a fresh :class:`TagIndex`."""
    by_id: dict[int, str] = {}
    by_name_lc: dict[str, int] = {}
    offset = 0
    total: Optional[int] = None
    while True:
        payload = await client.get_all_tags(offset=offset, limit=_TAG_PAGE_SIZE)
        if total is None:
            total = _total_items(payload)
        items = _extract_tags_from_payload(payload)
        if not items:
            break
        for tag in items:
            tag_id_raw = tag.get("tag_id")
            name_raw = tag.get("name")
            try:
                tag_id = int(tag_id_raw) if tag_id_raw is not None else None
            except (TypeError, ValueError):
                tag_id = None
            if tag_id is None or not isinstance(name_raw, str):
                continue
            name = name_raw.strip()
            if not name:
                continue
            by_id[tag_id] = name
            by_name_lc[name.lower()] = tag_id
        offset += len(items)
        if len(items) < _TAG_PAGE_SIZE:
            break
        if total is not None and offset >= total:
            break

    return TagIndex(
        by_id=by_id,
        by_name_lc=by_name_lc,
        expires_at=time.monotonic() + _TTL_SECONDS,
    )


async def get_tag_index(client: "EgoiClient") -> TagIndex:
    """Return a fresh-or-cached :class:`TagIndex` for *client*'s account."""
    key = _cache_key(client)
    cached = _CACHE.get(key)
    if cached is not None and cached.is_fresh():
        return cached
    lock = _get_lock(key)
    async with lock:
        # Double-check after acquiring the lock — another coroutine may
        # have populated the cache while we waited.
        cached = _CACHE.get(key)
        if cached is not None and cached.is_fresh():
            return cached
        index = await _load_tag_index(client)
        _CACHE[key] = index
        log.debug(
            "egoi tags: refreshed cache (%d tags, key=%s)", len(index.by_id), key
        )
        return index


def invalidate(client: "EgoiClient") -> None:
    """Drop the cached :class:`TagIndex` for *client*'s account.

    Intended hook for future tag-mutation actions (create/update/delete).
    Safe to call when nothing is cached.
    """
    _CACHE.pop(_cache_key(client), None)


def _format_unknown_tag_error(name: str, *, index: TagIndex) -> str:
    suggestions = difflib.get_close_matches(
        name, index.names(), n=_FUZZY_SUGGESTIONS, cutoff=0.6
    )
    msg = f"unknown tag name: {name!r}"
    if suggestions:
        msg += f" (did you mean: {', '.join(repr(s) for s in suggestions)}?)"
    elif index.by_id:
        sample = sorted(index.names(), key=str.lower)[:5]
        msg += f" — known tags include: {', '.join(repr(s) for s in sample)}"
    else:
        msg += " — no tags are defined in this E-goi account"
    return msg


def resolve_tag_value(value: Any, *, index: TagIndex) -> int:
    """Coerce *value* (int|str) into the canonical integer ``tag_id``.

    Raises :class:`ValueError` when the input is neither, when an integer
    id is not present in the index, or when a name has no exact (case-
    insensitive) match — in the last case the message includes up to
    three fuzzy suggestions when available.
    """
    if isinstance(value, bool):
        raise ValueError("tag must be int or string, not bool")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("tag id must be a positive integer")
        if index.by_id and value not in index.by_id:
            raise ValueError(f"unknown tag id: {value}")
        return value
    if isinstance(value, str):
        name = value.strip()
        if not name:
            raise ValueError("tag name must be a non-empty string")
        # Allow numeric strings ("7") as a convenience for the LLM, which
        # sometimes stringifies integers in tool calls.
        if name.isdigit():
            return resolve_tag_value(int(name), index=index)
        tag_id = index.by_name_lc.get(name.lower())
        if tag_id is None:
            raise ValueError(_format_unknown_tag_error(name, index=index))
        return tag_id
    raise ValueError(
        f"tag must be int or string; got {type(value).__name__}"
    )


async def resolve_tags(values: Any, *, client: "EgoiClient") -> list[int]:
    """Resolve a list of tag references to canonical ids in one shot.

    All invalid entries are collected and reported in a single
    :class:`ValueError` so the caller doesn't play whack-a-mole when a
    contact-import file has multiple typos.
    """
    if not isinstance(values, list):
        raise ValueError("'tags' must be a list of int|string")
    if not values:
        return []
    index = await get_tag_index(client)
    resolved: list[int] = []
    errors: list[str] = []
    for raw in values:
        try:
            resolved.append(resolve_tag_value(raw, index=index))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    return resolved


def annotate_tags(values: Any, *, index: TagIndex) -> Any:
    """Map a raw ``tags`` field onto ``[{tag_id, name}, ...]``.

    Tolerates:

    * ``None`` / missing → returned unchanged.
    * ``list[int]`` (typical) → annotated with looked-up names
      (``name=None`` for unknown ids).
    * ``list[dict]`` (older payloads with ``{tag_id, name}`` already
      present) → kept as-is, just normalising ``tag_id`` to int.
    * Anything else → returned unchanged so the normaliser is total.
    """
    if not isinstance(values, list):
        return values
    out: list[dict] = []
    for entry in values:
        if isinstance(entry, dict):
            tid_raw = entry.get("tag_id")
            try:
                tid = int(tid_raw) if tid_raw is not None else None
            except (TypeError, ValueError):
                tid = None
            name = entry.get("name")
            if tid is not None and not isinstance(name, str):
                name = index.by_id.get(tid)
            out.append({"tag_id": tid, "name": name})
            continue
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            out.append({"tag_id": entry, "name": index.by_id.get(entry)})
            continue
        if isinstance(entry, str):
            stripped = entry.strip()
            if stripped.isdigit():
                tid = int(stripped)
                out.append({"tag_id": tid, "name": index.by_id.get(tid)})
            else:
                out.append({
                    "tag_id": index.by_name_lc.get(stripped.lower()),
                    "name": stripped,
                })
            continue
        # Unknown shape — preserve verbatim so we don't lose data.
        out.append({"tag_id": None, "name": str(entry)})
    return out
