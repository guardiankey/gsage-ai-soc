"""gSage AI — E-goi query helpers (pure, no global state).

Normalisers, pagination iterators, retry classification, body-builders
and the shared tool config schema. All helpers operate on the JSON-decoded
``dict``/``list`` payloads returned by :class:`._client.EgoiClient`.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional

from src.mcp_server.tools.marketing.egoi._client import EgoiClient, EgoiError

log = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────

# Default + hard upper bounds for any list/search endpoint.
DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 500_000  # CSV-overflow ceiling (user-configured policy)

# Page size used when paginating with offset/limit. The API caps at 1000
# (per https://developers.e-goi.com — Get all contacts: limit [1..1000]).
# Larger pages drastically reduce request count for big lists; keep
# below the cap so we still have headroom for occasional smaller pages.
DEFAULT_PAGE_SIZE = 1000
MAX_PAGE_SIZE = 1000

# Rows shown inline to the LLM. Above this we auto-emit CSV.
AGENT_PREVIEW_ROWS_EGOI = 50

# Threshold above which contact_quick_action should NOT be used (and the
# corresponding ``maxItems`` constraint on its bulk-id arrays).
QUICK_ACTION_MAX_ITEMS = 10

# Maximum body size for /lists/{id}/contacts/actions/import-bulk. The API
# documents a 20 MB request limit; we stay below it with a safety margin.
IMPORT_BULK_MAX_BYTES = 18_000_000

# HTTP status codes that are safe to retry (transient transport errors).
EGOI_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset(
    {0, 408, 429, 500, 502, 503, 504}
)


def is_retryable_error(exc: EgoiError) -> bool:
    return exc.status_code in EGOI_RETRYABLE_STATUS_CODES


# JSON-Schema fragment accepted as a single contact id. Modern E-goi
# accounts return a 10-char hexadecimal hash; legacy lists may still
# expose a numeric id. Both forms are forwarded to the SDK as-is.
CONTACT_ID_SCHEMA: dict = {
    "oneOf": [
        {"type": "integer", "minimum": 1},
        {"type": "string", "minLength": 1, "maxLength": 64},
    ],
}


def normalize_contact_id(value: Any) -> Any:
    """Validate and return a contact id usable by the E-goi SDK.

    Accepts a positive integer or a non-empty string (the typical
    10-char hex hash). Returns the value unchanged on success and
    raises :class:`ValueError` otherwise.
    """
    if isinstance(value, bool):
        raise ValueError("contact_id must be int or string, not bool")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("contact_id integer must be positive")
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            raise ValueError("contact_id string must be non-empty")
        return v
    raise ValueError(
        f"contact_id must be int or string; got {type(value).__name__}"
    )


def normalize_contact_ids(values: Any) -> list[Any]:
    """Normalise a list of contact ids (each int or non-empty string)."""
    if not isinstance(values, list) or not values:
        raise ValueError("'contact_ids' must be a non-empty array")
    return [normalize_contact_id(v) for v in values]



def clamp_max_rows(value: Optional[int]) -> int:
    if value is None:
        return DEFAULT_MAX_ROWS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROWS
    if n <= 0:
        return DEFAULT_MAX_ROWS
    return min(n, HARD_MAX_ROWS)


def clamp_page_size(value: Optional[int]) -> int:
    if value is None:
        return DEFAULT_PAGE_SIZE
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if n <= 0:
        return DEFAULT_PAGE_SIZE
    return min(n, MAX_PAGE_SIZE)


# ── Response unwrappers ────────────────────────────────────────────────────


def unwrap_items(payload: Any) -> list[dict]:
    """Extract the ``items`` array from a paginated E-goi response.

    The API returns ``{"total_items": N, "items": [...], ...}`` for list
    endpoints. Some endpoints return a bare list. ``None`` / unexpected
    shapes resolve to an empty list.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []


def total_items(payload: Any) -> Optional[int]:
    """Return ``total_items`` from a paginated response, if reported."""
    if isinstance(payload, dict):
        val = payload.get("total_items")
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None
    return None


# ── Generic pagination ─────────────────────────────────────────────────────


PageFetcher = Callable[[int, int], Awaitable[Any]]


async def iter_all_pages(
    fetcher: PageFetcher,
    *,
    max_rows: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    normaliser: Optional[Callable[[dict], dict]] = None,
) -> tuple[list[dict], Optional[int]]:
    """Collect up to ``max_rows`` items by repeatedly calling ``fetcher``.

    ``fetcher`` must accept ``(offset, limit)`` and return the raw response
    payload (as returned by :class:`._client.EgoiClient` methods). Stops
    on the first page that returns fewer items than ``page_size`` or once
    ``max_rows`` is reached. Returns ``(items, server_total)``.
    """
    page_size = clamp_page_size(page_size)
    max_rows = max(1, int(max_rows or DEFAULT_MAX_ROWS))
    collected: list[dict] = []
    offset = 0
    server_total: Optional[int] = None
    while len(collected) < max_rows:
        remaining = max_rows - len(collected)
        page_limit = min(page_size, remaining)
        payload = await fetcher(offset, page_limit)
        if server_total is None:
            server_total = total_items(payload)
        page_items = unwrap_items(payload)
        if not page_items:
            break
        for item in page_items:
            collected.append(normaliser(item) if normaliser else item)
            if len(collected) >= max_rows:
                break
        if len(page_items) < page_limit:
            break
        offset += page_limit
    return collected, server_total


async def iter_all_pages_stream(
    fetcher: PageFetcher,
    *,
    max_rows: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    normaliser: Optional[Callable[[dict], dict]] = None,
) -> AsyncIterator[tuple[dict, Optional[int]]]:
    """Stream rows one-by-one across paginated fetches.

    Identical pagination semantics as :func:`iter_all_pages` but yields
    ``(row, server_total)`` tuples instead of returning the full list,
    so callers can persist rows incrementally (e.g. straight to a CSV
    file on disk) without holding the whole dataset in memory. The
    ``server_total`` value is the same on every yield (resolved on the
    first page) so consumers can use the last seen value.
    """
    page_size = clamp_page_size(page_size)
    max_rows = max(1, int(max_rows or DEFAULT_MAX_ROWS))
    yielded = 0
    offset = 0
    server_total: Optional[int] = None
    while yielded < max_rows:
        remaining = max_rows - yielded
        page_limit = min(page_size, remaining)
        payload = await fetcher(offset, page_limit)
        if server_total is None:
            server_total = total_items(payload)
        page_items = unwrap_items(payload)
        if not page_items:
            break
        for item in page_items:
            yield (normaliser(item) if normaliser else item), server_total
            yielded += 1
            if yielded >= max_rows:
                break
        if len(page_items) < page_limit:
            break
        offset += page_limit


# ── Normalisers ────────────────────────────────────────────────────────────


def _get_nested(d: Any, *path: str, default: Any = None) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def normalize_list(item: Any) -> dict:
    """Flatten an E-goi list resource into a tabular dict."""
    if not isinstance(item, dict):
        return {}
    stats = item.get("contact_stats") or {}
    return {
        "list_id": item.get("list_id"),
        "internal_name": item.get("internal_name"),
        "public_name": item.get("public_name"),
        "language": item.get("lang"),
        "created": item.get("created"),
        "updated": item.get("updated"),
        "contacts_active": stats.get("active") if isinstance(stats, dict) else None,
        "contacts_inactive": stats.get("inactive") if isinstance(stats, dict) else None,
        "contacts_removed": stats.get("removed") if isinstance(stats, dict) else None,
        "contacts_unconfirmed": stats.get("unconfirmed") if isinstance(stats, dict) else None,
    }


def _compact_extra(extra: Any) -> Any:
    """Drop empty values from an E-goi ``extra`` payload.

    The API echoes every extra field defined on the list — even when the
    contact has no value for that field. Keeping the empties bloats each
    contact row (~13 entries × ~40 bytes) and makes the LLM context cost
    explode on contact searches. We drop entries whose ``value`` is
    ``None`` / empty string while preserving the original shape (list of
    objects or dict).
    """
    def _is_empty(v: Any) -> bool:
        return v is None or (isinstance(v, str) and v.strip() == "")

    if isinstance(extra, list):
        compact: list[Any] = []
        for entry in extra:
            if isinstance(entry, dict):
                val = entry.get("value")
                if _is_empty(val):
                    continue
            compact.append(entry)
        return compact
    if isinstance(extra, dict):
        return {k: v for k, v in extra.items() if not _is_empty(v)}
    return extra


def normalize_contact(item: Any) -> dict:
    """Flatten an E-goi contact into a tabular dict.

    The API nests base fields under ``base`` and extra fields under
    ``extra``. We expose the most relevant base fields top-level and
    keep ``extra`` as a sub-object for ad-hoc inspection. Empty
    ``extra`` entries are stripped to keep payloads compact.
    """
    if not isinstance(item, dict):
        return {}
    base = item.get("base") or {}
    if not isinstance(base, dict):
        base = {}
    return {
        "contact_id": item.get("contact_id") or base.get("contact_id"),
        "list_id": item.get("list_id"),
        "status": item.get("status") or base.get("status"),
        "email": base.get("email"),
        "first_name": base.get("first_name"),
        "last_name": base.get("last_name"),
        "cellphone": base.get("cellphone"),
        "telephone": base.get("telephone"),
        "birth_date": base.get("birth_date"),
        "language": base.get("lang"),
        "created": item.get("created") or base.get("created"),
        "updated": item.get("updated") or base.get("updated"),
        "tags": item.get("tags") or base.get("tags"),
        "extra": _compact_extra(item.get("extra")),
    }


def normalize_campaign(item: Any) -> dict:
    """Flatten an E-goi campaign into a tabular dict."""
    if not isinstance(item, dict):
        return {}
    return {
        "campaign_hash": item.get("campaign_hash"),
        "internal_name": item.get("internal_name"),
        "subject": item.get("subject"),
        "type": item.get("type"),
        "status": item.get("status"),
        "group_id": item.get("group_id"),
        "list_id": item.get("list_id"),
        "send_date": item.get("send_date"),
        "created": item.get("created"),
        "updated": item.get("updated"),
    }


def normalize_campaign_group(item: Any) -> dict:
    """Flatten an E-goi campaign-group into a tabular dict."""
    if not isinstance(item, dict):
        return {}
    return {
        "group_id": item.get("group_id"),
        "name": item.get("name"),
        "created": item.get("created"),
        "updated": item.get("updated"),
    }


def normalize_segment(item: Any) -> dict:
    """Flatten an E-goi segment into a tabular dict."""
    if not isinstance(item, dict):
        return {}
    return {
        "segment_id": item.get("segment_id"),
        "name": item.get("name"),
        "type": item.get("type"),
        "list_id": item.get("list_id"),
        "created": item.get("created"),
        "updated": item.get("updated"),
        "contacts_count": item.get("contacts"),
    }


def normalize_email_report(item: Any) -> dict:
    """Pass-through normaliser for /reports/email/{hash} payloads."""
    if not isinstance(item, dict):
        return {}
    return dict(item)


# ── Email report aggregation helpers ───────────────────────────────────────


def iter_email_breakdown(report: Any, key: str) -> Iterable[dict]:
    """Yield rows from an EmailReport breakdown section.

    The current E-goi schema exposes breakdowns under bare keys
    (``date``, ``domain``, ``url``, ``reader``, ``location``, ``hour``,
    ``weekday``). Older schemas used a ``by_*`` prefix. We accept both
    so callers can pass either form.
    """
    if not isinstance(report, dict):
        return
    candidates: list[str] = [key]
    if key.startswith("by_"):
        candidates.append(key[3:])
    else:
        candidates.append(f"by_{key}")
    for candidate in candidates:
        section = report.get(candidate)
        if isinstance(section, list):
            for row in section:
                if isinstance(row, dict):
                    yield row
            return


# ── Config schema ──────────────────────────────────────────────────────────


EGOI_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["api_key"],
    "properties": {
        "api_key": {
            "type": "string",
            "format": "password",
            "sensitive": True,
            "description": (
                "E-goi API key. Created under My Account → API Keys in the "
                "E-goi web UI. Sent in the 'Apikey' HTTP header."
            ),
        },
        "host": {
            "type": "string",
            "description": (
                "API base URL (default: https://api.egoiapp.com). Override "
                "only when E-goi has provisioned a regional/dedicated host."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 600,
            "description": "Per-request timeout in seconds (default: 60).",
        },
    },
    "additionalProperties": False,
}


EGOI_CONFIG_DEFAULTS: dict = {
    "host": "https://api.egoiapp.com",
    "timeout": 60,
}


def build_client(config: dict) -> EgoiClient:
    """Instantiate :class:`EgoiClient` from a tool config dict."""
    return EgoiClient(
        api_key=str(config.get("api_key") or ""),
        host=str(config.get("host") or "https://api.egoiapp.com"),
        timeout=float(config.get("timeout") or 60),
    )
