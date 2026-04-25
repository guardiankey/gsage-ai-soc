"""gSage AI — Microsoft MSRC Patch Tuesday Bulletin tool."""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any, ClassVar, Optional

import httpx
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.cache.decorator import cached
from src.shared.elasticsearch.client import ElasticsearchClient
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
MSRC_CVRF_BASE = "https://api.msrc.microsoft.com/cvrf/v3.0/cvrf"
_CACHE_TTL_CURRENT_SECONDS: int = 12 * 3600       # 12 h for current month
_CACHE_TTL_ARCHIVE_SECONDS: int = 7 * 24 * 3600   # 7 days for past months
_MAX_RESULTS_HARD_LIMIT: int = 50
_TOOL_NAME: str = "msrc_bulletin"
_CVE_SEARCH_MONTHS: int = 3  # number of months searched when cve_id given without month

_SEVERITY_ORDER: dict[str, int] = {
    "Critical": 0,
    "Important": 1,
    "Moderate": 2,
    "Low": 3,
}

_MONTH_RE = re.compile(
    r"^\d{4}-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$"
)

# ── Per-coroutine session transport ───────────────────────────────────────────
_msrc_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar(
    "msrc_bulletin_session", default=None
)


# ── Month utilities ───────────────────────────────────────────────────────────

def _current_month_id() -> str:
    """Return the current month in MSRC format, e.g. '2026-Apr'."""
    return datetime.utcnow().strftime("%Y-%b")


def _recent_month_ids(n: int) -> list[str]:
    """Return the n most recent month IDs in MSRC format (current month first)."""
    result: list[str] = []
    d = datetime.utcnow()
    for _ in range(n):
        result.append(d.strftime("%Y-%b"))
        # Subtract one calendar month without dateutil dependency
        if d.month == 1:
            d = d.replace(year=d.year - 1, month=12, day=1)
        else:
            d = d.replace(month=d.month - 1, day=1)
    return result


def _ttl_for(month_id: str) -> int:
    """Return the appropriate cache TTL in seconds for a given month."""
    return (
        _CACHE_TTL_CURRENT_SECONDS
        if month_id == _current_month_id()
        else _CACHE_TTL_ARCHIVE_SECONDS
    )


# ── Defensive list coercion ───────────────────────────────────────────────────

def _ensure_list(value: Any) -> list:
    """Wrap a single object in a list; pass lists unchanged; return [] for None."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ── CVRF parser ───────────────────────────────────────────────────────────────

def _parse_cvrf(raw_json: dict) -> list[dict]:
    """Flatten a CVRF v3 JSON document into a list of simplified vulnerability dicts.

    Only the pre-processed list is stored in the cache, not the raw 5-20 MB payload.
    """
    # Product ID → name map
    product_map: dict[str, str] = {}
    for pn in _ensure_list(raw_json.get("ProductTree", {}).get("FullProductName")):
        pid = pn.get("ProductID", "")
        name = pn.get("Value", "")
        if pid:
            product_map[pid] = name

    entries: list[dict] = []

    for vuln in _ensure_list(raw_json.get("Vulnerability")):
        cve_id: str = vuln.get("CVE", "")
        title: str = (vuln.get("Title") or {}).get("Value", "")

        # Description: prefer Note Type=1 (Details), fallback to Type=0 (General)
        description = ""
        for note in _ensure_list(vuln.get("Notes")):
            if note.get("Type") == 1:
                description = (note.get("Value") or "")[:500]
                break
        if not description:
            for note in _ensure_list(vuln.get("Notes")):
                if note.get("Type") == 0:
                    description = (note.get("Value") or "")[:500]
                    break

        # CWE — may be a single object or a list in the API
        cwe_list: list[str] = []
        for cwe in _ensure_list(vuln.get("CWE")):
            cwe_id = cwe.get("ID", "")
            if cwe_id:
                cwe_list.append(cwe_id)

        # Threats: Type 0=impact, 1=exploit status, 3=severity
        severity = ""
        impact = ""
        exploited = False
        publicly_disclosed = False

        for threat in _ensure_list(vuln.get("Threats")):
            t_type = threat.get("Type", -1)
            val: str = ((threat.get("Description") or {}).get("Value") or "")

            if t_type == 0 and not impact:
                impact = val
            elif t_type == 1:
                # "Publicly Disclosed:No;Exploited:Yes;Latest Software Release:..."
                for segment in val.split(";"):
                    segment = segment.strip()
                    if ":" in segment:
                        k, v = segment.split(":", 1)
                        k_lower = k.strip().lower()
                        v_lower = v.strip().lower()
                        if k_lower == "exploited" and v_lower == "yes":
                            exploited = True
                        elif k_lower == "publicly disclosed" and v_lower == "yes":
                            publicly_disclosed = True
            elif t_type == 3 and not severity:
                severity = val

        # Remediations — Type 2 = official patch (KB articles)
        kb_articles: list[dict] = []
        for rem in _ensure_list(vuln.get("Remediations")):
            if rem.get("Type") == 2:
                kb_desc = ((rem.get("Description") or {}).get("Value") or "")
                kb_url = rem.get("URL", "") or ""
                kb_subtype = rem.get("SubType", "") or ""
                if kb_desc or kb_url:
                    kb_articles.append({
                        "kb": kb_desc,
                        "url": kb_url,
                        "subtype": kb_subtype,
                    })

        # CVSS — take the highest BaseScore across all per-product score sets
        max_cvss: float = 0.0
        cvss_vector: str = ""
        for cvss in _ensure_list(vuln.get("CVSSScoreSets")):
            try:
                score = float(cvss.get("BaseScore") or 0)
            except (ValueError, TypeError):
                score = 0.0
            if score > max_cvss:
                max_cvss = score
                cvss_vector = cvss.get("Vector", "") or ""

        # Affected products — derived from ProductStatuses
        seen_pids: set[str] = set()
        affected_product_ids: list[str] = []
        for ps in _ensure_list(vuln.get("ProductStatuses")):
            for pid in _ensure_list(ps.get("ProductID")):
                if pid and pid not in seen_pids:
                    seen_pids.add(pid)
                    affected_product_ids.append(pid)

        # Resolve product names; cap at 25 to keep cached payload manageable
        affected_products = [
            product_map.get(pid, pid) for pid in affected_product_ids[:25]
        ]

        entries.append({
            "cve_id": cve_id,
            "title": title,
            "description": description,
            "severity": severity,
            "impact": impact,
            "exploited": exploited,
            "publicly_disclosed": publicly_disclosed,
            "cvss_base_score": round(max_cvss, 1) if max_cvss > 0 else None,
            "cvss_vector": cvss_vector or None,
            "cwe": cwe_list,
            "affected_products": affected_products,
            "kb_articles": kb_articles,
        })

    return entries


# ── Cache helper ──────────────────────────────────────────────────────────────


@cached(
    ttl=_CACHE_TTL_ARCHIVE_SECONDS,
    scope="global",
    key_fn=lambda *, month_id, **_: f"msrc:bulletin:{month_id}:v1",
    ttl_fn=lambda _result, *, month_id, **_kw: _ttl_for(month_id),
    logical_name=_TOOL_NAME,
)
async def _get_bulletin(
    *,
    month_id: str,
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> list[dict]:
    """Fetch and cache the MSRC bulletin for the given month.

    Returns a pre-parsed list of vulnerability dicts. The TTL is 12 h for the
    current Patch Tuesday month and 7 days for archived months (handled by
    ``ttl_fn``).

    Raises:
        httpx.HTTPStatusError: 404 when the month is not yet published.
        httpx.RequestError: on network failure.
    """
    url = f"{MSRC_CVRF_BASE}/{month_id}"
    logger.info("Fetching MSRC CVRF bulletin %s from %s", month_id, url)

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"Accept": "application/json"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        raw_json = resp.json()

    parsed = _parse_cvrf(raw_json)
    logger.info(
        "MSRC bulletin %s parsed (%d vulnerabilities)",
        month_id,
        len(parsed),
    )
    return parsed


# ── Filtering & sorting ───────────────────────────────────────────────────────

def _filter_bulletins(
    entries: list[dict],
    severity: Optional[str],
    product: Optional[str],
    cve_id: Optional[str],
    keyword: Optional[str],
    exploited_only: bool,
    impact: Optional[str],
) -> list[dict]:
    """Return matching entries after applying all active filters."""
    cve_upper = cve_id.upper() if cve_id else None
    # Normalize to title-case to match API values (e.g. "critical" → "Critical")
    severity_norm = severity.capitalize() if severity else None

    results: list[dict] = []
    for entry in entries:
        if cve_upper and entry.get("cve_id", "").upper() != cve_upper:
            continue
        if severity_norm and entry.get("severity", "") != severity_norm:
            continue
        if exploited_only and not entry.get("exploited", False):
            continue
        if product:
            prod_lc = product.lower()
            if not any(prod_lc in p.lower() for p in entry.get("affected_products", [])):
                continue
        if impact:
            if impact.lower() not in entry.get("impact", "").lower():
                continue
        if keyword:
            kw = keyword.lower()
            haystack = (
                entry.get("title", "").lower()
                + " "
                + entry.get("description", "").lower()
            )
            if kw not in haystack:
                continue
        results.append(entry)

    return results


def _sort_entries(entries: list[dict]) -> list[dict]:
    """Sort by severity (Critical first), then by CVSS score descending."""
    return sorted(
        entries,
        key=lambda e: (
            _SEVERITY_ORDER.get(e.get("severity", ""), 99),
            -(e.get("cvss_base_score") or 0.0),
        ),
    )


# ── Tool class ────────────────────────────────────────────────────────────────

class MsrcBulletinTool(BaseTool):
    """Microsoft MSRC Patch Tuesday security bulletin lookup.

    Queries the public MSRC CVRF v3.0 API (no authentication required) for
    monthly Patch Tuesday security bulletins. Monthly data is pre-parsed and
    cached in PostgreSQL (12 h for the current month, 7 days for archives).

    Key use cases:
      - "What Critical patches were released this month?" → severity="Critical"
      - "Any actively exploited vulnerabilities in April 2026?" → month="2026-Apr",
        exploited_only=true
      - "Is CVE-2026-12345 in Patch Tuesday?" → cve_id="CVE-2026-12345"
      - "RCE patches for Windows this month?" → product="Windows",
        impact="Remote Code Execution"
      - "Show me March 2026 patches" → month="2026-Mar"
    """

    name: ClassVar[str] = "msrc_bulletin"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Microsoft Patch Tuesday security bulletin lookup for Windows CVEs and patch status"
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["security:read"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 45
    background_threshold_seconds: ClassVar[Optional[int]] = 20
    use_circuit_breaker: ClassVar[bool] = True
    requires_config: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": (
                    "Month to query in YYYY-Mon format (e.g. '2026-Apr'). "
                    "Defaults to the current month. "
                    "When cve_id is given without a month, the last 3 months "
                    "are searched automatically."
                ),
                "pattern": r"^\d{4}-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$",
            },
            "severity": {
                "type": "string",
                "description": "Filter by MSRC severity rating.",
                "enum": ["Critical", "Important", "Moderate", "Low"],
            },
            "product": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match on affected product names "
                    "(e.g. 'Windows', 'Exchange', 'Office', '.NET')."
                ),
            },
            "cve_id": {
                "type": "string",
                "description": (
                    "Exact CVE ID to look up (e.g. 'CVE-2026-12345'). "
                    "If month is not specified, the last 3 months are searched."
                ),
                "pattern": r"^CVE-\d{4}-\d{4,}$",
            },
            "keyword": {
                "type": "string",
                "description": "Keyword search in vulnerability title and description.",
            },
            "exploited_only": {
                "type": "boolean",
                "description": (
                    "If true, return only vulnerabilities confirmed as exploited "
                    "in the wild."
                ),
                "default": False,
            },
            "impact": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match on impact type "
                    "(e.g. 'Remote Code Execution', 'Elevation of Privilege', "
                    "'Denial of Service', 'Information Disclosure', "
                    "'Security Feature Bypass')."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum entries to return (default: 20, max: 50).",
                "default": 20,
                "minimum": 1,
                "maximum": 50,
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Session transport ─────────────────────────────────────────────────────

    async def run(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        redis_client: redis.Redis,
        es_client: ElasticsearchClient,
        gsage_session_id: Optional[uuid.UUID] = None,
    ) -> ToolResult:
        """Override to make the DB session available inside execute()."""
        token = _msrc_session_ctx.set(session)
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
            _msrc_session_ctx.reset(token)

    # ── Core execution ────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        # ── Extract parameters ────────────────────────────────────────────────
        month: Optional[str] = params.get("month") or None
        severity: Optional[str] = params.get("severity") or None
        product: Optional[str] = params.get("product") or None
        cve_id: Optional[str] = params.get("cve_id") or None
        keyword: Optional[str] = params.get("keyword") or None
        exploited_only: bool = bool(params.get("exploited_only", False))
        impact: Optional[str] = params.get("impact") or None
        max_results: int = min(
            int(params.get("max_results", 20)), _MAX_RESULTS_HARD_LIMIT
        )

        # ── Retrieve DB session from ContextVar ───────────────────────────────
        session = _msrc_session_ctx.get()
        if session is None:
            return self._failure(
                code="INTERNAL_ERROR",
                message="DB session not available in execution context.",
            )

        # ── Determine which months to query ───────────────────────────────────
        # CVE lookup without month → search last N months, stop at first hit
        if cve_id and not month:
            months_to_query = _recent_month_ids(_CVE_SEARCH_MONTHS)
            cve_search_mode = True
        else:
            months_to_query = [month or _current_month_id()]
            cve_search_mode = False

        # ── Fetch bulletins ───────────────────────────────────────────────────
        all_entries: list[dict] = []
        months_fetched: list[str] = []

        for mid in months_to_query:
            try:
                entries = await _get_bulletin(month_id=mid, session=session)
                all_entries.extend(entries)
                months_fetched.append(mid)

                # In CVE search mode stop as soon as we found at least one match
                if cve_search_mode and cve_id:
                    if any(
                        e.get("cve_id", "").upper() == cve_id.upper()
                        for e in entries
                    ):
                        break

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    if len(months_to_query) > 1:
                        # Skip unavailable months silently in multi-month mode
                        logger.debug(
                            "MSRC bulletin %s not found (404), skipping", mid
                        )
                        continue
                    return self._failure(
                        code="MONTH_NOT_FOUND",
                        message=(
                            f"MSRC bulletin for '{mid}' is not available yet "
                            f"or does not exist. Try a different month."
                        ),
                    )
                return self._failure(
                    code="FETCH_ERROR",
                    message=(
                        f"Failed to fetch MSRC bulletin for '{mid}': "
                        f"HTTP {exc.response.status_code}"
                    ),
                )
            except httpx.RequestError as exc:
                return self._failure(
                    code="FETCH_ERROR",
                    message=f"Network error fetching MSRC bulletin for '{mid}': {exc}",
                )

        total_in_bulletin = len(all_entries)

        # ── Filter ────────────────────────────────────────────────────────────
        filtered = _filter_bulletins(
            entries=all_entries,
            severity=severity,
            product=product,
            cve_id=cve_id,
            keyword=keyword,
            exploited_only=exploited_only,
            impact=impact,
        )
        filtered = _sort_entries(filtered)
        filtered_count = len(filtered)
        returned = filtered[:max_results]

        # ── Build filters summary ─────────────────────────────────────────────
        filters_applied: dict = {}
        if month:
            filters_applied["month"] = month
        if severity:
            filters_applied["severity"] = severity
        if product:
            filters_applied["product"] = product
        if cve_id:
            filters_applied["cve_id"] = cve_id
        if keyword:
            filters_applied["keyword"] = keyword
        if exploited_only:
            filters_applied["exploited_only"] = True
        if impact:
            filters_applied["impact"] = impact

        return self._success(
            data={
                "summary": {
                    "months_queried": months_fetched,
                    "total_in_bulletin": total_in_bulletin,
                    "filtered_count": filtered_count,
                    "returned_count": len(returned),
                    "filters_applied": filters_applied,
                },
                "vulnerabilities": returned,
            }
        )
