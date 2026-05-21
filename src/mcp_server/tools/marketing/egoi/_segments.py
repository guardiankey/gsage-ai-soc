"""gSage AI — E-goi segment resolver (id ↔ name) per list.

Mirror of :mod:`._tags` for segments, with one important difference:
segments are scoped to a single E-goi list, so the cache key includes
the ``list_id``.

* ``get_segment_index(client, list_id, *, org_id)`` — paginates
  ``client.get_all_segments(list_id)`` once per ``(org, api_key, list)``
  combination and caches the result for :data:`_TTL_SECONDS`. Uses the
  shared :func:`~src.shared.cache.decorator.cached` decorator
  (DB-backed, scope ``org``).
* ``resolve_segment_value(value, *, index)`` — coerces ``int`` (validated
  against the index) or ``str`` (case-insensitive name lookup) into a
  canonical integer ``segment_id``.
* ``annotate_segments(values, *, index)`` — same shape contract as
  :func:`._tags.annotate_tags`.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import _tool_session_ctx
from src.shared.cache.decorator import cached

if TYPE_CHECKING:
    from src.mcp_server.tools.marketing.egoi._client import EgoiClient

log = logging.getLogger(__name__)


_TTL_SECONDS = 600
_SEGMENT_PAGE_SIZE = 1000
_FUZZY_SUGGESTIONS = 3
_CACHE_KEY_PREFIX = "egoi:segments:v1"


@dataclass
class SegmentIndex:
    """Bidirectional segment mapping for one (E-goi account, list)."""

    list_id: int
    by_id: dict[int, str] = field(default_factory=dict)
    by_name_lc: dict[str, int] = field(default_factory=dict)
    # Optional metadata kept for the taxonomy tool's listing payload.
    meta: dict[int, dict] = field(default_factory=dict)

    def names(self) -> list[str]:
        return list(self.by_id.values())


def _api_key_hash(client: "EgoiClient") -> str:
    api_key = getattr(client, "_api_key", "") or ""
    if not api_key:
        return "anon"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _extract_segments(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [s for s in items if isinstance(s, dict)]
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)]
    return []


def _total_items(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        total = payload.get("total_items")
        if isinstance(total, int):
            return total
    return None


async def _load_segment_index_raw(
    client: "EgoiClient", list_id: int
) -> dict[str, Any]:
    """Paginate ``/lists/{id}/segments`` and return a JSON payload.

    Returns ``{"list_id": int, "by_id": {str: name}, "meta": {str: {...}}}``.
    Meta carries ``type``, ``contacts``, ``created``, ``updated`` so the
    taxonomy tool can render rich listings without a second fetch.
    """
    by_id: dict[str, str] = {}
    meta: dict[str, dict] = {}
    offset = 0
    total: Optional[int] = None
    while True:
        payload = await client.get_all_segments(
            list_id=list_id, offset=offset, limit=_SEGMENT_PAGE_SIZE
        )
        if total is None:
            total = _total_items(payload)
        items = _extract_segments(payload)
        if not items:
            break
        for seg in items:
            sid_raw = seg.get("segment_id")
            name_raw = seg.get("name")
            try:
                sid = int(sid_raw) if sid_raw is not None else None
            except (TypeError, ValueError):
                sid = None
            if sid is None or not isinstance(name_raw, str):
                continue
            name = name_raw.strip()
            if not name:
                continue
            by_id[str(sid)] = name
            meta[str(sid)] = {
                "type": seg.get("type"),
                "contacts": seg.get("contacts"),
                "created": seg.get("created"),
                "updated": seg.get("updated"),
            }
        offset += len(items)
        if len(items) < _SEGMENT_PAGE_SIZE:
            break
        if total is not None and offset >= total:
            break

    return {"list_id": int(list_id), "by_id": by_id, "meta": meta}


def _to_segment_index(payload: dict[str, Any]) -> SegmentIndex:
    raw_by_id = payload.get("by_id") or {}
    raw_meta = payload.get("meta") or {}
    by_id: dict[int, str] = {}
    by_name_lc: dict[str, int] = {}
    meta: dict[int, dict] = {}
    for k, v in raw_by_id.items():
        try:
            sid = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, str):
            continue
        by_id[sid] = v
        by_name_lc[v.lower()] = sid
        m = raw_meta.get(k)
        if isinstance(m, dict):
            meta[sid] = m
    list_id_raw = payload.get("list_id")
    try:
        list_id = int(list_id_raw) if list_id_raw is not None else 0
    except (TypeError, ValueError):
        list_id = 0
    return SegmentIndex(list_id=list_id, by_id=by_id, by_name_lc=by_name_lc, meta=meta)


@cached(
    ttl=_TTL_SECONDS,
    scope="org",
    key_fn=lambda *, client, list_id, **_: (
        f"{_CACHE_KEY_PREFIX}:{_api_key_hash(client)}:{int(list_id)}"
    ),
    logical_name="egoi_segments_index",
)
async def _fetch_segment_index_cached(
    *,
    client: "EgoiClient",
    list_id: int,
    org_id: uuid.UUID,  # noqa: ARG001 — consumed by @cached
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> dict[str, Any]:
    return await _load_segment_index_raw(client, list_id)


async def get_segment_index(
    client: "EgoiClient",
    list_id: int,
    *,
    org_id: Optional[uuid.UUID] = None,
) -> SegmentIndex:
    """Return a fresh-or-cached :class:`SegmentIndex` for *(client, list)*."""
    session = _tool_session_ctx.get()
    if org_id is not None and session is not None:
        payload = await _fetch_segment_index_cached(
            client=client, list_id=list_id, org_id=org_id, session=session
        )
    else:
        payload = await _load_segment_index_raw(client, list_id)
    return _to_segment_index(payload)


def _format_unknown_segment_error(name: str, *, index: SegmentIndex) -> str:
    suggestions = difflib.get_close_matches(
        name, index.names(), n=_FUZZY_SUGGESTIONS, cutoff=0.6
    )
    msg = f"unknown segment name: {name!r}"
    if suggestions:
        msg += f" (did you mean: {', '.join(repr(s) for s in suggestions)}?)"
    elif index.by_id:
        sample = sorted(index.names(), key=str.lower)[:5]
        msg += f" — known segments include: {', '.join(repr(s) for s in sample)}"
    else:
        msg += f" — no segments are defined for list {index.list_id}"
    return msg


def resolve_segment_value(value: Any, *, index: SegmentIndex) -> int:
    """Coerce *value* (int|str) into a canonical integer ``segment_id``."""
    if isinstance(value, bool):
        raise ValueError("segment must be int or string, not bool")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("segment id must be a positive integer")
        if index.by_id and value not in index.by_id:
            raise ValueError(f"unknown segment id: {value}")
        return value
    if isinstance(value, str):
        name = value.strip()
        if not name:
            raise ValueError("segment name must be a non-empty string")
        if name.isdigit():
            return resolve_segment_value(int(name), index=index)
        sid = index.by_name_lc.get(name.lower())
        if sid is None:
            raise ValueError(_format_unknown_segment_error(name, index=index))
        return sid
    raise ValueError(
        f"segment must be int or string; got {type(value).__name__}"
    )


def annotate_segments(values: Any, *, index: SegmentIndex) -> Any:
    """Map raw segment refs onto ``[{segment_id, name}, ...]``."""
    if not isinstance(values, list):
        return values
    out: list[dict] = []
    for entry in values:
        if isinstance(entry, dict):
            sid_raw = entry.get("segment_id")
            try:
                sid = int(sid_raw) if sid_raw is not None else None
            except (TypeError, ValueError):
                sid = None
            name = entry.get("name")
            if sid is not None and not isinstance(name, str):
                name = index.by_id.get(sid)
            out.append({"segment_id": sid, "name": name})
            continue
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            out.append({"segment_id": entry, "name": index.by_id.get(entry)})
            continue
        if isinstance(entry, str):
            stripped = entry.strip()
            if stripped.isdigit():
                sid = int(stripped)
                out.append({"segment_id": sid, "name": index.by_id.get(sid)})
            else:
                out.append({
                    "segment_id": index.by_name_lc.get(stripped.lower()),
                    "name": stripped,
                })
            continue
        out.append({"segment_id": None, "name": str(entry)})
    return out
