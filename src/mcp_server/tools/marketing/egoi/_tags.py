"""gSage AI — E-goi tag resolver (id ↔ name).

The E-goi API references tags by integer ``tag_id`` everywhere — both in
``GET /lists/{id}/contacts`` payloads and in ``attach_tag``/``detach_tag``
bodies. Humans (and LLM agents) think in tag *names*, so this module
provides a small bidirectional cache:

* ``get_tag_index(client, *, org_id)`` — paginates ``client.get_all_tags()``
  once and caches the result for :data:`_TTL_SECONDS` per organisation via
  the shared :func:`~src.shared.cache.decorator.cached` decorator
  (DB-backed, scope ``org``). The session is pulled from
  :data:`~src.mcp_server.tools.base._tool_session_ctx`; when no session is
  available (e.g. unit tests), the loader runs without persisting a cache
  entry.
* ``resolve_tag_value(value, *, index)`` — accepts ``int`` (validated
  against the index) or ``str`` (case-insensitive name lookup) and
  returns the canonical ``int`` ``tag_id``. Raises :class:`ValueError`
  with a fuzzy hint (`difflib.get_close_matches`) when the name is
  unknown.
* ``resolve_tags(values, *, client, org_id)`` — convenience wrapper that
  fetches the index once and surfaces *all* invalid inputs in a single
  error.
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


# Refresh cached entries after this many seconds. Tags rarely change
# during a single tool run; a 10-minute TTL keeps the system responsive
# to ad-hoc edits in the E-goi UI without hammering ``/tags``.
_TTL_SECONDS = 600

# Page size used when paginating ``get_all_tags`` (the endpoint caps at
# the standard E-goi ``limit`` ceiling of 1000).
_TAG_PAGE_SIZE = 1000

# Number of suggestions surfaced when a tag name is not found.
_FUZZY_SUGGESTIONS = 3

# Logical-key prefix in ``gsage_tool_cache`` rows. Bumping the version
# suffix forces a refresh across deployments.
_CACHE_KEY_PREFIX = "egoi:tags:v1"


@dataclass
class TagIndex:
    """Bidirectional tag mapping for one E-goi account."""

    by_id: dict[int, str] = field(default_factory=dict)
    by_name_lc: dict[str, int] = field(default_factory=dict)

    def names(self) -> list[str]:
        """Return all known tag names (original case) for fuzzy hints."""
        return list(self.by_id.values())


def _api_key_hash(client: "EgoiClient") -> str:
    """Stable, non-secret discriminator for the client's API key.

    Two distinct E-goi accounts can be configured under the same gSage
    organisation (multi-config tools). Including a hash of the API key
    in the logical cache key keeps their tag indices isolated.
    """
    api_key = getattr(client, "_api_key", "") or ""
    if not api_key:
        return "anon"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


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


async def _load_tag_index_raw(client: "EgoiClient") -> dict[str, Any]:
    """Paginate ``/tags`` and return a JSON-serialisable index payload.

    Returns ``{"by_id": {str(tag_id): name, ...}}``. The string keys keep
    the payload compatible with :func:`json.dumps` (the cache layer
    serialises results); :func:`_to_tag_index` rehydrates ints.
    """
    by_id: dict[str, str] = {}
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
            by_id[str(tag_id)] = name
        offset += len(items)
        if len(items) < _TAG_PAGE_SIZE:
            break
        if total is not None and offset >= total:
            break

    return {"by_id": by_id}


def _to_tag_index(payload: dict[str, Any]) -> TagIndex:
    """Rehydrate a cached payload (string keys) into a :class:`TagIndex`."""
    raw_by_id = payload.get("by_id") or {}
    by_id: dict[int, str] = {}
    by_name_lc: dict[str, int] = {}
    for k, v in raw_by_id.items():
        try:
            tid = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, str):
            continue
        by_id[tid] = v
        by_name_lc[v.lower()] = tid
    return TagIndex(by_id=by_id, by_name_lc=by_name_lc)


@cached(
    ttl=_TTL_SECONDS,
    scope="org",
    key_fn=lambda *, client, **_: f"{_CACHE_KEY_PREFIX}:{_api_key_hash(client)}",
    logical_name="egoi_tags_index",
)
async def _fetch_tag_index_cached(
    *,
    client: "EgoiClient",
    org_id: uuid.UUID,  # noqa: ARG001 — consumed by @cached (scope="org")
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> dict[str, Any]:
    """Cached wrapper around :func:`_load_tag_index_raw`."""
    return await _load_tag_index_raw(client)


async def get_tag_index(
    client: "EgoiClient",
    *,
    org_id: Optional[uuid.UUID] = None,
) -> TagIndex:
    """Return a fresh-or-cached :class:`TagIndex` for *client*'s account.

    Pass *org_id* to enable the shared DB-backed cache (scope ``org``).
    When *org_id* is ``None`` or the calling code has no DB session
    available, the index is fetched without persistence — useful for
    unit tests and one-off scripts.
    """
    session = _tool_session_ctx.get()
    if org_id is not None and session is not None:
        payload = await _fetch_tag_index_cached(
            client=client, org_id=org_id, session=session
        )
    else:
        payload = await _load_tag_index_raw(client)
    return _to_tag_index(payload)


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


async def resolve_tags(
    values: Any,
    *,
    client: "EgoiClient",
    org_id: Optional[uuid.UUID] = None,
) -> list[int]:
    """Resolve a list of tag references to canonical ids in one shot.

    All invalid entries are collected and reported in a single
    :class:`ValueError` so the caller doesn't play whack-a-mole when a
    contact-import file has multiple typos.
    """
    if not isinstance(values, list):
        raise ValueError("'tags' must be a list of int|string")
    if not values:
        return []
    index = await get_tag_index(client, org_id=org_id)
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
