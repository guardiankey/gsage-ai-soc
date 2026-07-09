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
from src.mcp_server.tools.result_export import build_agent_payload, summarize
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
# Defensive cap on the number of rows we will materialise / write to the CSV
# artifact, mirroring the pattern in GLPI (_CSV_FETCH_LIMIT). The full CISA
# KEV catalogue is ~1.5 k entries so this is generous, but it protects against
# pathological filter combinations or feed growth.
_CSV_FETCH_LIMIT: int = 10_000
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


# ── Enrichment (cross-reference with MSRC) ────────────────────────────────────
async def _enrich_with_msrc_crossref(
    rows: list[dict],
    *,
    session: AsyncSession,
) -> list[dict]:
    """Annotate KEV rows with ``in_msrc`` (bool) when vendor is Microsoft.

    Microsoft CVEs are grouped by the YYYY-Mon derived from ``dateAdded``
    so we only call :func:`_get_bulletin` once per relevant month (each
    call is cached for 12 h–7 d by ``msrc_bulletin._get_bulletin``).
    Non-Microsoft rows are annotated with ``in_msrc=False`` without any
    external call. Failures fetching a month are non-fatal: rows for that
    month are simply left with ``in_msrc=None``.
    """
    # Late import to avoid a circular dependency with msrc_bulletin.
    from src.mcp_server.tools.soc.threat_intel.msrc_bulletin import (  # noqa: PLC0415
        _get_bulletin,
    )

    month_to_cves: dict[str, set[str]] = {}
    ms_row_indices: list[int] = []

    for idx, row in enumerate(rows):
        vendor = (row.get("vendor") or "").lower()
        if "microsoft" not in vendor:
            row["in_msrc"] = False
            continue
        ms_row_indices.append(idx)
        date_str = row.get("dateAdded") or ""
        try:
            month_id = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%b")
        except ValueError:
            row["in_msrc"] = None
            continue
        month_to_cves.setdefault(month_id, set()).add(
            (row.get("cveID") or "").upper()
        )

    # Fetch each month at most once and build a CVE → True set.
    msrc_cves: set[str] = set()
    failed_months: list[str] = []
    for month_id in month_to_cves:
        try:
            entries = await _get_bulletin(month_id=month_id, session=session)
        except Exception as exc:  # noqa: BLE001
            # MSRC may not have published the month yet (404) or the call
            # may have failed transiently — degrade gracefully.
            logger.debug(
                "cisa_kev cross-ref: failed to fetch MSRC %s: %s",
                month_id, exc,
            )
            failed_months.append(month_id)
            continue
        for entry in entries:
            cve = (entry.get("cve_id") or "").upper()
            if cve:
                msrc_cves.add(cve)

    for idx in ms_row_indices:
        if rows[idx].get("in_msrc") is None:
            # Already marked None above (unparseable date) — leave it.
            continue
        cve = (rows[idx].get("cveID") or "").upper()
        if cve in msrc_cves:
            rows[idx]["in_msrc"] = True
        else:
            # Could be a legitimate miss or a failed month — mark None in
            # the latter case so the agent can tell.
            try:
                row_month = datetime.strptime(
                    rows[idx].get("dateAdded") or "", "%Y-%m-%d"
                ).strftime("%Y-%b")
            except ValueError:
                row_month = ""
            rows[idx]["in_msrc"] = (
                None if row_month in failed_months else False
            )

    return rows


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

    Bulk / CSV output:
      - ``max_results`` controls **only** how many rows are inlined in the
        response (preview); it does **not** cap the CSV.
      - Set ``export_csv=true`` to receive every filtered row as a CSV
        artifact (up to a defensive 10 000-row cap). The same artifact is
        also produced automatically whenever the filtered set exceeds the
        inline preview cap, so the agent always has a downloadable file
        for large result sets.
      - When the CSV is produced, Microsoft rows are enriched with an
        ``in_msrc`` flag (cross-reference to the MSRC Patch Tuesday bulletin
        of the same month). Non-Microsoft rows get ``in_msrc=false`` with no
        extra call.
      - After receiving the artifact (``artifacts.csv_file.file_id``), the
        agent should use the ``csv_query`` / ``csv_describe`` / ``csv_edit``
        / ``csv_join`` tools to analyse the full dataset instead of asking
        for more rows inline.
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
                "description": (
                    "Maximum number of rows shipped inline in the response "
                    "(default: 20, max: 50). This does NOT cap the CSV "
                    "artifact: when ``export_csv=true`` (or when the "
                    "filtered set exceeds the inline preview), every "
                    "filtered row is written to the CSV up to a defensive "
                    "10 000-row hard limit."
                ),
                "default": 20,
                "minimum": 1,
                "maximum": 50,
            },
            "export_csv": {
                "type": "boolean",
                "description": (
                    "If true, persist the full filtered result set as a CSV "
                    "artifact (returned in ``artifacts.csv_file``). The CSV "
                    "includes the ``in_msrc`` cross-reference column for "
                    "Microsoft rows. Use the csv_query / csv_describe / "
                    "csv_edit / csv_join tools to analyse the file. The "
                    "CSV is also produced automatically whenever the "
                    "filtered set is larger than the inline preview, "
                    "regardless of this flag."
                ),
                "default": False,
            },
            "group_by": {
                "type": "array",
                "description": (
                    "Optional list of result columns to aggregate in the "
                    "``summary.top`` block (e.g. ['vendor', 'product']). "
                    "Defaults to ['vendor', 'product']."
                ),
                "items": {"type": "string"},
            },
            "top_n": {
                "type": "integer",
                "description": (
                    "Number of top values per ``group_by`` column shown in "
                    "``summary.top`` (default: 10)."
                ),
                "default": 10,
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
        tool_call_id: Optional[uuid.UUID] = None,
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
        export_csv: bool = bool(params.get("export_csv", False))
        group_by_param = params.get("group_by") or None
        top_n: int = int(params.get("top_n", 10))

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

        # Defensive cap on what we materialise into the CSV / formatted rows
        # to avoid pathological payloads.
        truncated_for_csv = filtered_count > _CSV_FETCH_LIMIT
        capped = filtered[:_CSV_FETCH_LIMIT]
        formatted_rows = [_format_entry(v) for v in capped]

        # Decide whether a CSV artifact will be produced. The CSV is forced
        # when the caller asked for it OR when the filtered set is larger
        # than the inline preview (max_results), since the agent cannot
        # show every row inline anyway.
        will_generate_csv = export_csv or filtered_count > max_results

        # Enrichment (cross-reference with MSRC) only when the dataset is
        # going to be shipped as a CSV — keeps small inline queries fast.
        session = _session_ctx.get()
        if will_generate_csv and formatted_rows and session is not None:
            try:
                formatted_rows = await _enrich_with_msrc_crossref(
                    formatted_rows, session=session
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cisa_kev: MSRC cross-ref enrichment failed: %s", exc
                )

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
        if export_csv:
            filters_applied["export_csv"] = True

        # ── Build agent payload (preview + optional CSV artifact) ────────────
        # preview_rows = min(max_results, hard limit). ``build_agent_payload``
        # will force CSV generation whenever rows_total > preview_rows.
        agent_payload = await build_agent_payload(
            tool=self,
            rows=formatted_rows,
            export_csv=export_csv,
            export_json=False,
            filename_prefix=f"{self.name}_search",
            agent_context=agent_context,
            preview_rows=max_results,
        )

        # ── Top-N summary over the full (post-enrichment) result ─────────────
        summary_group_by = group_by_param or ["vendor", "product"]
        agg_summary = summarize(
            formatted_rows,
            group_by=summary_group_by,
            top_n=top_n,
            sample_size=0,  # sample is redundant with rows_preview
        )
        ransomware_count = sum(
            1 for r in formatted_rows if r.get("ransomware") == "Known"
        )
        in_msrc_count = sum(
            1 for r in formatted_rows if r.get("in_msrc") is True
        )

        return self._success(
            data={
                "summary": {
                    "total_in_catalog": total_in_catalog,
                    "filtered_count": filtered_count,
                    "returned_count": len(agent_payload["rows_preview"]),
                    "csv_truncated": truncated_for_csv,
                    "csv_row_limit": _CSV_FETCH_LIMIT,
                    "catalog_version": catalog_version,
                    "filters_applied": filters_applied,
                    "ransomware_count": ransomware_count,
                    "in_msrc_count": in_msrc_count,
                    "aggregations": agg_summary,
                },
                "rows_total": agent_payload["rows_total"],
                "rows_overflow": agent_payload["rows_overflow"],
                "rows_preview_limit": max_results,
                "artifacts": agent_payload["artifacts"],
                "agent_hint": agent_payload["agent_hint"],
                "vulnerabilities": agent_payload["rows_preview"],
            }
        )
