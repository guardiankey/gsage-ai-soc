"""gSage AI — CISA Known Exploited Vulnerabilities (KEV) tool."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from typing import ClassVar, Optional

import httpx
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.cache.decorator import cached
from src.shared.elasticsearch.client import ElasticsearchClient
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)
_CACHE_TTL_SECONDS: int = 6 * 3600  # 6 hours
_MAX_RESULTS_HARD_LIMIT: int = 50
_TOOL_NAME: str = "cisa_kev"

# ── Per-coroutine session transport ───────────────────────────────────────────
# ContextVar makes the DB session safely accessible inside execute(), which does
# not receive `session` as a parameter.
_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar(
    "cisa_kev_session", default=None
)


# ── Cache helpers ─────────────────────────────────────────────────────────────
@cached(
    ttl=_CACHE_TTL_SECONDS,
    scope="global",
    key_fn=lambda *_args, **_kwargs: "cisa_kev:feed:v1",
    logical_name=_TOOL_NAME,
)
async def _get_kev_feed(
    *,
    session: AsyncSession,  # noqa: ARG001 — required by @cached; used inside decorator
) -> dict:
    """Fetch the CISA KEV feed, backed by GSageToolCache (GLOBAL, 6 h TTL).

    Returns a dict with keys ``vulnerabilities`` (list) and ``catalogVersion`` (str).
    """
    logger.info("Downloading CISA KEV feed from %s", CISA_KEV_URL)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(CISA_KEV_URL)
        resp.raise_for_status()
        payload = resp.json()

    vulns: list[dict] = payload.get("vulnerabilities", [])
    version: str = payload.get("catalogVersion", "unknown")
    logger.info("CISA KEV feed fetched (%d vulnerabilities)", len(vulns))
    return {"vulnerabilities": vulns, "catalogVersion": version}


# ── Filtering ─────────────────────────────────────────────────────────────────
def _filter_kevs(
    vulns: list[dict],
    days: Optional[int],
    vendor: Optional[str],
    product: Optional[str],
    cve_id: Optional[str],
    keyword: Optional[str],
    ransomware_only: bool,
    cwe: Optional[str],
) -> list[dict]:
    """Apply all active filters, returning matching KEV entries."""
    cutoff: Optional[date] = None
    if days is not None and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    cwe_upper = cwe.upper() if cwe else None

    results: list[dict] = []
    for v in vulns:
        # ── Exact CVE ID
        if cve_id is not None:
            if v.get("cveID", "").upper() != cve_id.upper():
                continue

        # ── Date added
        if cutoff is not None:
            date_str = v.get("dateAdded", "")
            try:
                date_added = date.fromisoformat(date_str)
            except ValueError:
                continue
            if date_added < cutoff:
                continue

        # ── Vendor (partial, case-insensitive)
        if vendor is not None:
            if vendor.lower() not in v.get("vendorProject", "").lower():
                continue

        # ── Product (partial, case-insensitive)
        if product is not None:
            if product.lower() not in v.get("product", "").lower():
                continue

        # ── Keyword in name + description
        if keyword is not None:
            kw = keyword.lower()
            haystack = (
                v.get("vulnerabilityName", "").lower()
                + " "
                + v.get("shortDescription", "").lower()
            )
            if kw not in haystack:
                continue

        # ── Ransomware campaigns
        if ransomware_only and v.get("knownRansomwareCampaignUse") != "Known":
            continue

        # ── CWE
        if cwe_upper is not None:
            if cwe_upper not in [c.upper() for c in v.get("cwes", [])]:
                continue

        results.append(v)

    return results


def _format_entry(v: dict) -> dict:
    """Return a concise KEV entry dict."""
    return {
        "cveID": v.get("cveID"),
        "vendor": v.get("vendorProject"),
        "product": v.get("product"),
        "name": v.get("vulnerabilityName"),
        "dateAdded": v.get("dateAdded"),
        "dueDate": v.get("dueDate"),
        "ransomware": v.get("knownRansomwareCampaignUse"),
        "cwes": v.get("cwes", []),
        "description": v.get("shortDescription"),
    }


# ── Tool class ────────────────────────────────────────────────────────────────
class CisaKevTool(BaseTool):
    """CISA Known Exploited Vulnerabilities (KEV) Catalog lookup.

    Searches the CISA KEV catalog — an authoritative list of CVEs confirmed to
    be actively exploited in the wild. The full catalog (~1500+ entries) is
    cached in PostgreSQL for 6 hours to avoid repeated downloads.

    Key use cases:
      - "What new KEVs were added this week?" → days=7
      - "Any exploited Microsoft vulnerabilities lately?" → vendor="Microsoft", days=30
      - "Is CVE-2024-0012 in the KEV?" → cve_id="CVE-2024-0012"
      - "RCE KEVs linked to ransomware?" → keyword="remote code execution",
        ransomware_only=true
    """

    name: ClassVar[str] = "cisa_kev"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Query the CISA Known Exploited Vulnerabilities catalog to check if a CVE is actively exploited"
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["security:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_config: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": (
                    "Return KEVs added in the last N days (default: 7). "
                    "Set to 0 to disable the date filter — requires at least "
                    "one other filter to avoid dumping the full catalog."
                ),
                "default": 7,
                "minimum": 0,
            },
            "vendor": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match on vendor/project name "
                    "(e.g. 'Microsoft', 'Cisco', 'Apache')."
                ),
            },
            "product": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match on product name "
                    "(e.g. 'Windows', 'FortiOS', 'Confluence')."
                ),
            },
            "cve_id": {
                "type": "string",
                "description": "Exact CVE ID to look up (e.g. 'CVE-2024-0012').",
                "pattern": r"^CVE-\d{4}-\d{4,}$",
            },
            "keyword": {
                "type": "string",
                "description": (
                    "Keyword search in vulnerability name and short description "
                    "(e.g. 'remote code execution', 'authentication bypass')."
                ),
            },
            "ransomware_only": {
                "type": "boolean",
                "description": (
                    "If true, return only KEVs linked to known ransomware campaigns."
                ),
                "default": False,
            },
            "cwe": {
                "type": "string",
                "description": "Filter by CWE ID (e.g. 'CWE-89', 'CWE-78').",
                "pattern": r"^CWE-\d+$",
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
        token = _session_ctx.set(session)
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
            _session_ctx.reset(token)

    # ── Core execution ────────────────────────────────────────────────────────
    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        # ── Extract and validate parameters ───────────────────────────────────
        days: int = int(params.get("days", 7))
        vendor: Optional[str] = params.get("vendor") or None
        product: Optional[str] = params.get("product") or None
        cve_id: Optional[str] = params.get("cve_id") or None
        keyword: Optional[str] = params.get("keyword") or None
        ransomware_only: bool = bool(params.get("ransomware_only", False))
        cwe: Optional[str] = params.get("cwe") or None
        max_results: int = min(
            int(params.get("max_results", 20)), _MAX_RESULTS_HARD_LIMIT
        )

        # Require at least one filter when days=0 to avoid dumping the full
        # catalog (1500+ entries) into the agent context.
        has_filter = any([vendor, product, cve_id, keyword, ransomware_only, cwe])
        if days == 0 and not has_filter:
            return self._failure(
                code="NO_FILTER",
                message=(
                    "When 'days' is 0, at least one of vendor, product, cve_id, "
                    "keyword, ransomware_only, or cwe must be specified to avoid "
                    "returning the entire KEV catalog."
                ),
            )

        # ── Retrieve feed (cached) ─────────────────────────────────────────────
        session = _session_ctx.get()
        if session is None:
            return self._failure(
                code="INTERNAL_ERROR",
                message="DB session not available in execution context.",
            )

        try:
            feed = await _get_kev_feed(session=session)
            vulns = feed["vulnerabilities"]
            catalog_version = feed["catalogVersion"]
        except httpx.HTTPStatusError as exc:
            return self._failure(
                code="FETCH_ERROR",
                message=(
                    f"Failed to download CISA KEV feed: HTTP {exc.response.status_code}"
                ),
            )
        except httpx.RequestError as exc:
            return self._failure(
                code="FETCH_ERROR",
                message=f"Network error downloading CISA KEV feed: {exc}",
            )

        total_in_catalog = len(vulns)

        # ── Filter ────────────────────────────────────────────────────────────
        filtered = _filter_kevs(
            vulns=vulns,
            days=days if days > 0 else None,
            vendor=vendor,
            product=product,
            cve_id=cve_id,
            keyword=keyword,
            ransomware_only=ransomware_only,
            cwe=cwe,
        )

        # Sort newest first
        filtered.sort(key=lambda v: v.get("dateAdded", ""), reverse=True)

        filtered_count = len(filtered)
        returned = filtered[:max_results]

        # ── Build filters_applied summary ─────────────────────────────────────
        filters_applied: dict = {}
        if days > 0:
            filters_applied["days"] = days
        if vendor:
            filters_applied["vendor"] = vendor
        if product:
            filters_applied["product"] = product
        if cve_id:
            filters_applied["cve_id"] = cve_id
        if keyword:
            filters_applied["keyword"] = keyword
        if ransomware_only:
            filters_applied["ransomware_only"] = True
        if cwe:
            filters_applied["cwe"] = cwe

        return self._success(
            data={
                "summary": {
                    "total_in_catalog": total_in_catalog,
                    "filtered_count": filtered_count,
                    "returned_count": len(returned),
                    "catalog_version": catalog_version,
                    "filters_applied": filters_applied,
                },
                "vulnerabilities": [_format_entry(v) for v in returned],
            }
        )
