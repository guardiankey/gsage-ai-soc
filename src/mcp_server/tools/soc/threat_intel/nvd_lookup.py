"""gSage AI — NVD (National Vulnerability Database) CVE / CPE lookup tool."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any, ClassVar, Optional

import httpx
import nvdlib  # type: ignore[import-untyped]
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.cache.decorator import cached
from src.shared.elasticsearch.client import ElasticsearchClient
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
_TOOL_NAME: str = "nvd_lookup"
_CACHE_TTL_SECONDS: int = 7 * 24 * 3600   # 7 days — CVE/CPE data is stable
_MAX_RESULTS: int = 50
_DAILY_BUDGET: int = 500                   # soft daily limit (conservative)
_WINDOW_SECONDS: int = 30                  # NVD sliding window
_WINDOW_LIMIT_WITH_KEY: int = 45           # safe margin below 50/30s
_WINDOW_LIMIT_NO_KEY: int = 4              # safe margin below 5/30s
_DELAY_WITH_KEY: float = 0.7              # nvdlib delay (minimum 0.6)
_DELAY_NO_KEY: float = 6.0               # nvdlib delay without API key

# Direct NVD API v2 endpoint (used instead of nvdlib for CVE operations)
_NVD_CVE_API: str = "https://services.nvd.nist.gov/rest/json/cves/2.0"
# NVD API v2 boolean flags — must appear without a value in the query string
_NVD_BOOLEAN_PARAMS: frozenset[str] = frozenset({
    "keywordExactMatch", "isVulnerable", "noRejected",
    "hasCertAlerts", "hasCertNotes", "hasKev", "hasOval",
})

# ── Per-coroutine session transport ───────────────────────────────────────────
# ContextVar makes the DB session safely accessible inside execute(), which does
# not receive `session` as a parameter.  Set by run() override.
_nvd_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar(
    "nvd_lookup_session", default=None
)


# ── Date normalisation ─────────────────────────────────────────────────────────

_NVD_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _parse_nvd_date(date_str: str) -> str:
    """Normalise an arbitrary date string to NVD API v2 format.

    NVD API v2 requires ``yyyy-MM-dd'T'HH:mm:ss.SSS +00:00``.  Input strings
    without a timezone are treated as UTC.  Strings that cannot be parsed are
    returned unchanged (the API will reject them with a clear 400/404 error
    rather than silently producing wrong results).
    """
    date_str = date_str.strip()
    for fmt in _NVD_DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S.000 +00:00")
        except ValueError:
            continue
    logger.warning("nvd_lookup: could not parse date %r — passing as-is", date_str)
    return date_str


# ── Cache / rate-limit signalling ─────────────────────────────────────────────


class _NvdRateLimited(Exception):
    """Raised by the cached fetcher when the daily budget or 30 s window is full.

    Propagates through the ``@cached`` wrapper so the caller can return a
    ``RATE_LIMIT_EXCEEDED`` failure without polluting the cache.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ── Rate-limit helpers ─────────────────────────────────────────────────────────

def _check_and_reserve_budget(state: dict, has_key: bool) -> tuple[bool, Optional[str]]:
    """Validate daily budget and 30-second sliding window; increment counters in-place.

    Returns:
        (allowed: bool, reason: str | None)
    """
    now_ts = time.time()

    # ── Daily soft limit ──────────────────────────────────────────────────
    daily_used: int = int(state.get("nvd_daily_used", 0))
    if daily_used >= _DAILY_BUDGET:
        return False, (
            f"Daily NVD API budget exhausted ({daily_used}/{_DAILY_BUDGET}). "
            "Budget resets at midnight UTC."
        )

    # ── 30-second sliding window ──────────────────────────────────────────
    window_count: int = int(state.get("window_30s_count", 0))
    window_start: float = float(state.get("window_30s_start_ts", 0.0))
    window_limit: int = _WINDOW_LIMIT_WITH_KEY if has_key else _WINDOW_LIMIT_NO_KEY

    if now_ts - window_start > _WINDOW_SECONDS:
        # Window expired — reset
        window_count = 0
        window_start = now_ts

    if window_count >= window_limit:
        wait_secs = int(_WINDOW_SECONDS - (now_ts - window_start)) + 1
        return False, (
            f"NVD rate limit: {window_count}/{window_limit} requests in the last "
            f"{_WINDOW_SECONDS}s. Retry in ~{wait_secs}s."
        )

    # ── Commit increments ─────────────────────────────────────────────────
    state["nvd_daily_used"] = daily_used + 1
    state["window_30s_count"] = window_count + 1
    state["window_30s_start_ts"] = window_start
    return True, None


# ── CVE normalization ──────────────────────────────────────────────────────────

def _description_en(obj: Any) -> str:
    """Extract the English description from a CVE or CPE object."""
    descs = getattr(obj, "descriptions", None) or []
    for d in descs:
        if getattr(d, "lang", "") == "en":
            return getattr(d, "value", "")
    if descs:
        return getattr(descs[0], "value", "")
    return ""


def _cwe_list(obj: Any) -> list[str]:
    """Extract CWE IDs from a CVE object's weaknesses attribute."""
    weaknesses = getattr(obj, "weaknesses", None) or []
    seen: list[str] = []
    for w in weaknesses:
        for desc in getattr(w, "description", None) or []:
            val = getattr(desc, "value", "")
            if val and val not in seen:
                seen.append(val)
    return seen


def _cpe_list(obj: Any) -> list[str]:
    """Extract CPE names from a CVE's configurations/cpe attribute."""
    cpes = getattr(obj, "cpe", None) or []
    return [getattr(c, "criteria", "") for c in cpes if getattr(c, "criteria", "")]


def _cvss_score(obj: Any) -> dict:
    """Return the best available CVSS score info (v3.1 > v3.0 > v2)."""
    score_list = getattr(obj, "score", None)
    if score_list and len(score_list) >= 3:
        version, value, severity = score_list[0], score_list[1], score_list[2]
        # Determine vector string
        for attr in ("v31vector", "v30vector", "v2vector"):
            vector = getattr(obj, attr, None)
            if vector:
                break
        else:
            vector = None
        return {
            "version": version,
            "score": value,
            "severity": severity,
            "vector": vector,
        }
    return {"version": None, "score": None, "severity": None, "vector": None}


def _normalize_cve(obj: Any) -> dict:
    """Return a concise, normalized dict from a nvdlib CVE object."""
    cvss = _cvss_score(obj)
    return {
        "id": getattr(obj, "id", None),
        "published": getattr(obj, "published", None),
        "lastModified": getattr(obj, "lastModified", None),
        "vulnStatus": getattr(obj, "vulnStatus", None),
        "description": _description_en(obj),
        "cvss_version": cvss["version"],
        "cvss_score": cvss["score"],
        "cvss_severity": cvss["severity"],
        "cvss_vector": cvss["vector"],
        "cwe": _cwe_list(obj),
        "cpe": _cpe_list(obj),
        "kev": getattr(obj, "exploitAdd", None) is not None,
        "url": getattr(obj, "url", None),
    }


def _normalize_cpe(obj: Any) -> dict:
    """Return a concise, normalized dict from a nvdlib CPE object."""
    titles = getattr(obj, "titles", None) or []
    title_en = ""
    for t in titles:
        if getattr(t, "lang", "") == "en":
            title_en = getattr(t, "value", "")
            break
    if not title_en and titles:
        title_en = getattr(titles[0], "value", "")

    deprecated_by: list[str] = []
    for dep in getattr(obj, "deprecatedBy", None) or []:
        name = getattr(dep, "cpeName", None) or getattr(dep, "cpeNameId", None)
        if name:
            deprecated_by.append(name)

    return {
        "cpeName": getattr(obj, "cpeName", None),
        "cpeNameId": getattr(obj, "cpeNameId", None),
        "deprecated": getattr(obj, "deprecated", False),
        "title": title_en,
        "created": getattr(obj, "created", None),
        "lastModifiedDate": getattr(obj, "lastModifiedDate", None),
        "deprecatedBy": deprecated_by,
    }


def _normalize_match(obj: Any) -> dict:
    """Return a concise, normalized dict from a nvdlib MatchString object."""
    matches: list[str] = [
        getattr(m, "cpeName", "") for m in (getattr(obj, "matches", None) or [])
    ]
    return {
        "matchCriteriaId": getattr(obj, "matchCriteriaId", None),
        "criteria": getattr(obj, "criteria", None),
        "status": getattr(obj, "status", None),
        "lastModifiedDate": getattr(obj, "lastModifiedDate", None),
        "matches": [m for m in matches if m],
    }


# ── Raw NVD API v2 normaliser (used by httpx-based CVE fetcher) ───────────────

def _normalize_cve_raw(cve: dict) -> dict:
    """Normalize a raw NVD API v2 CVE dict into a concise output dict."""
    description = ""
    for d in cve.get("descriptions", []):
        if d.get("lang") == "en":
            description = d.get("value", "")
            break
    if not description:
        for d in cve.get("descriptions", []):
            description = d.get("value", "")
            break

    cvss_version = cvss_score = cvss_severity = cvss_vector = None
    metrics = cve.get("metrics", {})
    for metric_key, fallback_version in (
        ("cvssMetricV31", "3.1"),
        ("cvssMetricV40", "4.0"),
        ("cvssMetricV30", "3.0"),
        ("cvssMetricV2", "2.0"),
    ):
        entries = metrics.get(metric_key)
        if entries:
            cd = entries[0].get("cvssData", {})
            cvss_version = cd.get("version", fallback_version)
            cvss_score = cd.get("baseScore")
            cvss_severity = cd.get("baseSeverity")
            cvss_vector = cd.get("vectorString")
            break

    cwe: list[str] = []
    for w in cve.get("weaknesses", []):
        for d in w.get("description", []):
            val = d.get("value", "")
            if val and val not in cwe:
                cwe.append(val)

    cpe: list[str] = []
    for cfg in cve.get("configurations", []):
        for node in cfg.get("nodes", []):
            for match in node.get("cpeMatch", []):
                criteria = match.get("criteria", "")
                if criteria and criteria not in cpe:
                    cpe.append(criteria)

    cve_id = cve.get("id", "")
    return {
        "id": cve_id,
        "published": cve.get("published"),
        "lastModified": cve.get("lastModified"),
        "vulnStatus": cve.get("vulnStatus"),
        "description": description,
        "cvss_version": cvss_version,
        "cvss_score": cvss_score,
        "cvss_severity": cvss_severity,
        "cvss_vector": cvss_vector,
        "cwe": cwe,
        "cpe": cpe,
        "kev": False,
        "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}" if cve_id else None,
    }


async def _nvd_fetch_cves(params: dict, api_key: Optional[str]) -> dict:
    """Fetch CVE data directly from the NVD API v2 using httpx.

    Bypasses nvdlib to avoid the ``Content-Type: application/json`` header that
    nvdlib sends on GET requests, which can cause 404 responses from the NVD
    API CDN/WAF layer.  Boolean flags (e.g. ``keywordExactMatch``) are sent
    without a ``=value`` suffix, as required by the NVD API v2 spec.

    If the NVD API responds with 404 and ``message: Invalid apiKey.`` (in the
    response headers), the request is automatically retried without the key so
    that the tool keeps working with degraded rate limits rather than failing
    entirely.  A warning is logged so operators know to rotate the key.
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["apiKey"] = api_key

    # Build query string manually so boolean flags appear without "=value"
    parts: list[str] = []
    for k, v in params.items():
        if k in _NVD_BOOLEAN_PARAMS and v:
            parts.append(k)
        elif v is not None:
            parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
    query = "&".join(parts)
    url = f"{_NVD_CVE_API}?{query}" if query else _NVD_CVE_API

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        logger.debug("NVD CVE request: GET %s", url)
        resp = await client.get(url, headers=headers)

        # NVD returns 404 with header 'message: Invalid apiKey.' for bad/expired keys.
        # Fall back to unauthenticated request so the tool keeps working.
        if resp.status_code == 404 and api_key:
            nvd_msg = resp.headers.get("message", "")
            if "invalid api" in nvd_msg.lower():
                logger.warning(
                    "NVD API key rejected (%r) — retrying without key. "
                    "Please rotate TOOL_NVD_LOOKUP__API_KEY.",
                    nvd_msg,
                )
                fallback_headers = {"Accept": "application/json"}
                resp = await client.get(url, headers=fallback_headers)

        resp.raise_for_status()
        return resp.json()


# ── Operation dispatchers ──────────────────────────────────────────────────────
# CVE operations use httpx directly (avoids nvdlib's problematic headers).
# CPE operations still use nvdlib (synchronous — wrapped in asyncio.to_thread).


async def _op_cve_by_id(cve_id: str, api_key: Optional[str]) -> list[dict]:
    """Fetch a single CVE by its CVE ID via direct NVD API v2 call."""
    data = await _nvd_fetch_cves({"cveId": cve_id}, api_key)
    return [
        _normalize_cve_raw(v["cve"])
        for v in (data.get("vulnerabilities") or [])
        if "cve" in v
    ]


async def _op_search_cve(
    query_params: dict,
    api_key: Optional[str],
    limit: int,
) -> list[dict]:
    """Search CVEs via direct NVD API v2 call."""
    nvd_params = {**query_params, "resultsPerPage": min(limit, 2000)}
    data = await _nvd_fetch_cves(nvd_params, api_key)
    vulns = data.get("vulnerabilities") or []
    return [
        _normalize_cve_raw(v["cve"])
        for v in vulns[:limit]
        if "cve" in v
    ]


async def _op_cpe_by_id(
    cpe_name_id: str,
    key: Optional[str],
    delay: Optional[float],
) -> list[dict]:
    """Fetch a single CPE by its UUID (cpeNameId)."""
    kwargs: dict[str, Any] = {"cpeNameId": cpe_name_id}
    if key:
        kwargs["key"] = key
        if delay is not None:
            kwargs["delay"] = delay
    raw: list[Any] = await asyncio.to_thread(nvdlib.searchCPE, **kwargs)
    return [_normalize_cpe(c) for c in raw]


async def _op_search_cpe(
    query_params: dict,
    key: Optional[str],
    delay: Optional[float],
    limit: int,
) -> list[dict]:
    """Search CPEs with arbitrary nvdlib.searchCPE parameters."""
    kwargs: dict[str, Any] = {"limit": limit}
    if key:
        kwargs["key"] = key
        if delay is not None:
            kwargs["delay"] = delay
    kwargs.update(query_params)
    raw: list[Any] = await asyncio.to_thread(nvdlib.searchCPE, **kwargs)
    return [_normalize_cpe(c) for c in raw]


async def _op_cpe_match(
    query_params: dict,
    key: Optional[str],
    delay: Optional[float],
) -> list[dict]:
    """Search CPE match strings via nvdlib.searchCPEmatch."""
    kwargs: dict[str, Any] = {}
    if key:
        kwargs["key"] = key
        if delay is not None:
            kwargs["delay"] = delay
    kwargs.update(query_params)
    raw: list[Any] = await asyncio.to_thread(nvdlib.searchCPEmatch, **kwargs)
    return [_normalize_match(m) for m in raw]


# ── Unified cached dispatcher ─────────────────────────────────────────────────


@cached(
    ttl=_CACHE_TTL_SECONDS,
    scope="global",
    key_fn=lambda *, operation, cache_params, **_: json.dumps(
        {"op": operation, "params": cache_params}, sort_keys=True
    ),
    logical_name=_TOOL_NAME,
)
async def _nvd_fetch_cached(
    *,
    operation: str,
    cache_params: dict,  # noqa: ARG001 — only used by key_fn
    nvd_params: dict,
    state: dict,
    has_key: bool,
    api_key: Optional[str],
    delay: float,
    limit: int,
    cve_id: Optional[str],
    cpe_name_id: Optional[str],
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> dict:
    """Enforce budget/window and dispatch to the right NVD operation helper.

    Returns a cacheable dict ``{operation, count, results}``. Raises
    ``_NvdRateLimited`` before any API call if the budget or sliding window is
    exhausted (so cache HITs are free). Propagates ``LookupError`` from nvdlib
    for empty-result signalling.
    """
    allowed, reason = _check_and_reserve_budget(state, has_key)
    if not allowed:
        raise _NvdRateLimited(reason or "NVD rate limit exceeded.")

    if operation == "cve_by_id":
        results = await _op_cve_by_id(cve_id=cve_id, api_key=api_key or None)  # type: ignore[arg-type]
    elif operation == "search_cve":
        results = await _op_search_cve(
            query_params=nvd_params, api_key=api_key or None, limit=limit
        )
    elif operation == "cpe_by_id":
        results = await _op_cpe_by_id(
            cpe_name_id=cpe_name_id,  # type: ignore[arg-type]
            key=api_key or None,
            delay=delay if has_key else None,
        )
    elif operation == "search_cpe":
        results = await _op_search_cpe(
            query_params=nvd_params,
            key=api_key or None,
            delay=delay if has_key else None,
            limit=limit,
        )
    else:  # cpe_match
        results = await _op_cpe_match(
            query_params=nvd_params,
            key=api_key or None,
            delay=delay if has_key else None,
        )

    return {"operation": operation, "count": len(results), "results": results}


# ── Tool class ─────────────────────────────────────────────────────────────────


class NvdLookupTool(BaseTool):
    """NVD (National Vulnerability Database) CVE and CPE lookup.

    Queries the NIST NVD API v2 using the ``nvdlib`` library.  Supports five
    operations via the ``operation`` parameter:

    * ``cve_by_id``   — Fetch a single CVE by its ID (e.g. CVE-2017-0144)
    * ``search_cve``  — Search CVEs by keyword, CPE name, severity, date range
    * ``cpe_by_id``   — Fetch a single CPE by its UUID (cpeNameId)
    * ``search_cpe``  — Search CPE names by keyword or partial match string
    * ``cpe_match``   — Retrieve CPE match criteria strings for a CVE or criteria

    Results are cached globally for 7 days to avoid redundant API calls.

    Rate limits (tracked in tool state, reset daily):
        - Without API key: 5 req / 30 s  →  conservatively 4/30s
        - With API key   : 50 req / 30 s →  conservatively 45/30s
        - Daily soft cap : 500 requests

    Configure the NVD API key (free) per-org via the admin console under
    *Tool Configuration → nvd_lookup → api_key*, or globally via the environment
    variable ``TOOL_NVD_LOOKUP__API_KEY``.  The tool works without a key but
    at a much lower rate.

    Permission: ``threat:intel``
    """

    name: ClassVar[str] = "nvd_lookup"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Query the NIST NVD to look up CVEs and CPEs by ID, keyword, severity, "
        "CPE name or date range"
    )
    category: ClassVar[str] = "threat_intel"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["threat:intel"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60   # nvdlib delay=6s means single calls take ~6s
    use_circuit_breaker: ClassVar[bool] = True
    requires_config: ClassVar[bool] = False

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "description": (
                    "The NVD query to perform:\n"
                    "- 'cve_by_id': fetch a single CVE (requires cve_id)\n"
                    "- 'search_cve': search CVEs by keyword, cpe_name, severity, or dates\n"
                    "- 'cpe_by_id': fetch a single CPE by UUID (requires cpe_name_id)\n"
                    "- 'search_cpe': search CPE entries by keyword or partial match string\n"
                    "- 'cpe_match': retrieve CPE match criteria (requires cve_id or cpe_match_string)"
                ),
                "enum": ["cve_by_id", "search_cve", "cpe_by_id", "search_cpe", "cpe_match"],
            },
            # ── CVE parameters ────────────────────────────────────────────
            "cve_id": {
                "type": "string",
                "description": (
                    "CVE identifier (e.g. 'CVE-2017-0144'). "
                    "Required for operation='cve_by_id'. "
                    "Also used in 'cpe_match' to filter match strings by CVE."
                ),
                "pattern": r"^CVE-\d{4}-\d{4,}$",
            },
            "cpe_name": {
                "type": "string",
                "description": (
                    "Full CPE 2.3 name to filter CVEs "
                    "(e.g. 'cpe:2.3:a:microsoft:exchange_server:2013:*:*:*:*:*:*:*'). "
                    "Used in 'search_cve'."
                ),
            },
            "keyword_search": {
                "type": "string",
                "description": (
                    "Keyword to search in CVE descriptions or CPE titles "
                    "(e.g. 'Microsoft Exchange', 'remote code execution'). "
                    "Used in 'search_cve' and 'search_cpe'."
                ),
            },
            "keyword_exact_match": {
                "type": "boolean",
                "description": (
                    "Require the keyword to appear as an exact phrase. "
                    "Used in 'search_cve'."
                ),
                "default": False,
            },
            "cvss_v3_severity": {
                "type": "string",
                "description": "Filter CVEs by CVSS v3 severity. Used in 'search_cve'.",
                "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
            },
            "pub_start_date": {
                "type": "string",
                "description": (
                    "Publication start date in ISO 8601 format "
                    "(e.g. '2024-01-01 00:00'). "
                    "Must be used with pub_end_date. "
                    "Maximum range: 120 days. Used in 'search_cve'."
                ),
            },
            "pub_end_date": {
                "type": "string",
                "description": (
                    "Publication end date in ISO 8601 format "
                    "(e.g. '2024-03-01 00:00'). "
                    "Must be used with pub_start_date. Used in 'search_cve'."
                ),
            },
            "last_mod_start_date": {
                "type": "string",
                "description": (
                    "Last-modified start date in ISO 8601 format. "
                    "Must be used with last_mod_end_date. Used in 'search_cve' and 'search_cpe'."
                ),
            },
            "last_mod_end_date": {
                "type": "string",
                "description": (
                    "Last-modified end date in ISO 8601 format. "
                    "Must be used with last_mod_start_date. Used in 'search_cve' and 'search_cpe'."
                ),
            },
            "is_vulnerable": {
                "type": "boolean",
                "description": (
                    "When combined with cpe_name, limit CVEs to those where "
                    "the CPE is marked as vulnerable. Used in 'search_cve'."
                ),
            },
            "source_identifier": {
                "type": "string",
                "description": (
                    "Filter CVEs by source contact (e.g. 'cve@mitre.org'). "
                    "Used in 'search_cve'."
                ),
            },
            # ── CPE parameters ────────────────────────────────────────────
            "cpe_name_id": {
                "type": "string",
                "description": (
                    "UUID of a specific CPE entry "
                    "(e.g. 'DC0A1B46-3B8D-45F2-8B9A-00000000001'). "
                    "Required for operation='cpe_by_id'."
                ),
            },
            "cpe_match_string": {
                "type": "string",
                "description": (
                    "Partial CPE match string "
                    "(e.g. 'cpe:2.3:a:microsoft:exchange_server:2013:'). "
                    "Used in 'search_cpe' and 'cpe_match'."
                ),
            },
            # ── Match criteria parameters ─────────────────────────────────
            "match_criteria_id": {
                "type": "string",
                "description": (
                    "UUID of a specific CPE match criteria entry. "
                    "Used in 'cpe_match' to retrieve a single match string."
                ),
            },
            # ── Common parameters ─────────────────────────────────────────
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default: 20, max: 50).",
                "default": 20,
                "minimum": 1,
                "maximum": 50,
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "api_key": {
                "type": "string",
                "description": (
                    "NVD API key — https://nvd.nist.gov/developers/request-an-api-key. "
                    "Optional but strongly recommended: raises rate limit from 5 to "
                    "50 requests per 30 seconds."
                ),
            },
        },
    }
    config_defaults: ClassVar[dict] = {"api_key": ""}

    audit_field_mapping: ClassVar[dict] = {"api_key": "sensitive"}

    state_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "nvd_daily_used": {
                "type": "integer",
                "description": "NVD API requests used today (resets at midnight UTC)",
                "default": 0,
            },
            "window_30s_count": {
                "type": "integer",
                "description": "Requests made within the current 30-second window",
                "default": 0,
            },
            "window_30s_start_ts": {
                "type": "number",
                "description": "Unix timestamp when the current 30-second window started",
                "default": 0.0,
            },
        },
    }
    state_defaults: ClassVar[dict] = {
        "nvd_daily_used": 0,
        "window_30s_count": 0,
        "window_30s_start_ts": 0.0,
    }
    reset_policy: ClassVar[str] = "daily"

    # ── Session transport ──────────────────────────────────────────────────────
    async def run(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        redis_client: redis.Redis,
        es_client: ElasticsearchClient,
        gsage_session_id: Optional[uuid.UUID] = None,
        tool_call_id: Optional[uuid.UUID] = None,
    ) -> ToolResult:
        """Override to make the DB session available inside execute()."""
        token = _nvd_session_ctx.set(session)
        try:
            return await super().run(
                agent_context,
                params,
                session,
                redis_client,
                es_client,
                gsage_session_id,
            )
        finally:
            _nvd_session_ctx.reset(token)

    # ── Core execution ─────────────────────────────────────────────────────────
    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """
        Params (all optional except ``operation`` and operation-specific required ones):
            operation (str, required): One of cve_by_id, search_cve, cpe_by_id, search_cpe, cpe_match.
            cve_id (str): CVE ID — required for cve_by_id; optional filter in cpe_match.
            cpe_name (str): CPE 2.3 name — used in search_cve.
            keyword_search (str): Keyword — used in search_cve / search_cpe.
            keyword_exact_match (bool): Exact keyword match — used in search_cve.
            cvss_v3_severity (str): LOW/MEDIUM/HIGH/CRITICAL — used in search_cve.
            pub_start_date / pub_end_date (str): Publication date range (ISO 8601).
            last_mod_start_date / last_mod_end_date (str): Modification date range.
            is_vulnerable (bool): Limit to vulnerable CPE — used in search_cve.
            source_identifier (str): Source contact filter — used in search_cve.
            cpe_name_id (str): CPE UUID — required for cpe_by_id.
            cpe_match_string (str): Partial CPE match string — used in search_cpe / cpe_match.
            match_criteria_id (str): Match criteria UUID — used in cpe_match.
            limit (int): Max results (1–50, default 20).
        """
        # ── Retrieve session ───────────────────────────────────────────────
        session = _nvd_session_ctx.get()
        if session is None:
            return self._failure(
                code="INTERNAL_ERROR",
                message="DB session not available in execution context.",
            )

        # ── Extract parameters ─────────────────────────────────────────────
        operation: str = str(params.get("operation", "")).strip()
        api_key: str = (config.get("api_key") or "").strip()
        has_key: bool = bool(api_key)
        delay: float = _DELAY_WITH_KEY if has_key else _DELAY_NO_KEY
        limit: int = min(int(params.get("limit", 20)), _MAX_RESULTS)

        # ── Validate operation ─────────────────────────────────────────────
        valid_ops = {"cve_by_id", "search_cve", "cpe_by_id", "search_cpe", "cpe_match"}
        if operation not in valid_ops:
            return self._failure(
                code="INVALID_INPUT",
                message=(
                    f"Unknown operation '{operation}'. "
                    f"Valid operations: {', '.join(sorted(valid_ops))}."
                ),
            )

        # ── Validate required params per operation ─────────────────────────
        cve_id: Optional[str] = (params.get("cve_id") or "").strip() or None
        cpe_name_id: Optional[str] = (params.get("cpe_name_id") or "").strip() or None

        if operation == "cve_by_id" and not cve_id:
            return self._failure(
                code="INVALID_INPUT",
                message="operation='cve_by_id' requires 'cve_id' parameter.",
            )
        if operation == "cpe_by_id" and not cpe_name_id:
            return self._failure(
                code="INVALID_INPUT",
                message="operation='cpe_by_id' requires 'cpe_name_id' parameter.",
            )

        # ── Build canonical cache key ──────────────────────────────────────
        cache_params: dict = {"operation": operation}
        if cve_id:
            cache_params["cve_id"] = cve_id.upper()
        if cpe_name_id:
            cache_params["cpe_name_id"] = cpe_name_id
        for key_name in (
            "cpe_name", "keyword_search", "keyword_exact_match",
            "cvss_v3_severity", "pub_start_date", "pub_end_date",
            "last_mod_start_date", "last_mod_end_date", "is_vulnerable",
            "source_identifier", "cpe_match_string", "match_criteria_id",
        ):
            val = params.get(key_name)
            if val is not None:
                cache_params[key_name] = val
        if operation in ("search_cve", "search_cpe"):
            cache_params["limit"] = limit

        # ── Build NVD query params for the specific operation ──────────────
        nvd_params: dict = {}
        if operation == "search_cve":
            if params.get("cpe_name"):
                nvd_params["cpeName"] = params["cpe_name"]
            if params.get("keyword_search"):
                nvd_params["keywordSearch"] = params["keyword_search"]
            if params.get("keyword_exact_match"):
                nvd_params["keywordExactMatch"] = True
            if params.get("cvss_v3_severity"):
                nvd_params["cvssV3Severity"] = params["cvss_v3_severity"]
            if params.get("pub_start_date"):
                nvd_params["pubStartDate"] = _parse_nvd_date(params["pub_start_date"])
            if params.get("pub_end_date"):
                nvd_params["pubEndDate"] = _parse_nvd_date(params["pub_end_date"])
            if params.get("last_mod_start_date"):
                nvd_params["lastModStartDate"] = _parse_nvd_date(params["last_mod_start_date"])
            if params.get("last_mod_end_date"):
                nvd_params["lastModEndDate"] = _parse_nvd_date(params["last_mod_end_date"])
            if params.get("is_vulnerable") is True:
                nvd_params["isVulnerable"] = True
            if params.get("source_identifier"):
                nvd_params["sourceIdentifier"] = params["source_identifier"]
            if not nvd_params:
                return self._failure(
                    code="INVALID_INPUT",
                    message=(
                        "operation='search_cve' requires at least one filter: "
                        "cpe_name, keyword_search, cvss_v3_severity, pub_start_date, "
                        "last_mod_start_date, is_vulnerable, or source_identifier."
                    ),
                )
        elif operation == "search_cpe":
            if params.get("keyword_search"):
                nvd_params["keywordSearch"] = params["keyword_search"]
            if params.get("cpe_match_string"):
                nvd_params["cpeMatchString"] = params["cpe_match_string"]
            if params.get("cpe_name_id"):
                nvd_params["cpeNameId"] = params["cpe_name_id"]
            if params.get("last_mod_start_date"):
                nvd_params["lastModStartDate"] = _parse_nvd_date(params["last_mod_start_date"])
            if params.get("last_mod_end_date"):
                nvd_params["lastModEndDate"] = _parse_nvd_date(params["last_mod_end_date"])
            if not nvd_params:
                return self._failure(
                    code="INVALID_INPUT",
                    message=(
                        "operation='search_cpe' requires at least one filter: "
                        "keyword_search, cpe_match_string, cpe_name_id, or date range."
                    ),
                )
        elif operation == "cpe_match":
            if cve_id:
                nvd_params["cveId"] = cve_id
            if params.get("cpe_match_string"):
                nvd_params["matchStringSearch"] = params["cpe_match_string"]
            if params.get("match_criteria_id"):
                nvd_params["matchCriteriaId"] = params["match_criteria_id"]
            if not nvd_params:
                return self._failure(
                    code="INVALID_INPUT",
                    message=(
                        "operation='cpe_match' requires at least one of: "
                        "cve_id, cpe_match_string, or match_criteria_id."
                    ),
                )

        # ── Cached dispatch (cache lookup + rate-limit + API call) ────────
        daily_before = int(state.get("nvd_daily_used", 0))
        try:
            cacheable = await _nvd_fetch_cached(
                operation=operation,
                cache_params=cache_params,
                nvd_params=nvd_params,
                state=state,
                has_key=has_key,
                api_key=api_key,
                delay=delay,
                limit=limit,
                cve_id=cve_id,
                cpe_name_id=cpe_name_id,
                session=session,
            )
        except _NvdRateLimited as exc:
            return self._failure(
                code="RATE_LIMIT_EXCEEDED",
                message=exc.message,
                retryable=True,
            )
        except LookupError:
            # nvdlib raises LookupError for empty results on some endpoints
            return self._success(
                data={
                    "operation": operation,
                    "count": 0,
                    "results": [],
                    "cached": False,
                    "daily_used": state.get("nvd_daily_used", 0),
                }
            )
        except Exception as exc:
            exc_str = str(exc)
            exc_type = type(exc).__name__
            is_rate = "429" in exc_str or "rate" in exc_str.lower()
            is_server = any(
                code in exc_str for code in ("500", "502", "503", "504")
            )
            is_not_found = "404" in exc_str

            # Extract 'message' header from httpx response (NVD sends errors there)
            nvd_resp_msg: str = ""
            resp_obj = getattr(exc, "response", None)
            if resp_obj is not None:
                nvd_resp_msg = getattr(resp_obj, "headers", {}).get("message", "")

            logger.warning(
                "NVD API error for operation=%s [%s]: %s%s",
                operation, exc_type, exc_str,
                f" | NVD message: {nvd_resp_msg}" if nvd_resp_msg else "",
                exc_info=True,
            )
            msg = f"NVD API error [{exc_type}]: {exc_str}"
            if nvd_resp_msg:
                msg += f" | NVD: {nvd_resp_msg}"
            if is_not_found and not nvd_resp_msg:
                msg += (
                    " — The NVD API returned 404. Possible causes: "
                    "(1) CVE/CPE ID not yet in the NVD database; "
                    "(2) rate limiting (NVD sometimes returns 404 instead of 429); "
                    "(3) invalid or expired TOOL_NVD_LOOKUP__API_KEY; "
                    "(4) network/DNS issue inside the container."
                )
            return self._failure(
                code="API_ERROR",
                message=msg,
                retryable=is_rate or is_server,
            )

        # ── Build response ─────────────────────────────────────────────────
        # Budget counter unchanged => response came from cache.
        was_cached = int(state.get("nvd_daily_used", 0)) == daily_before
        response: dict = {
            **cacheable,
            "cached": was_cached,
            "daily_used": state.get("nvd_daily_used", 0),
        }
        return self._success(data=response)
