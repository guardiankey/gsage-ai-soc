"""gSage AI — GDN GuardianKey tool.

Queries the GuardianKey GDN REST API to retrieve
auth-security analytics: dashboard report objects, usage summaries, and
per-authgroup breakdowns.

Supported actions:
  - ``query``         — Retrieve a specific dashboard report with optional filters.
  - ``list_reports``  — List available report names and their descriptions (no API call).
  - ``usage_summary`` — Get total event/user counts and per-authgroup breakdown.

Required permission: ``gdn:read``
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, ClassVar, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult, _tool_session_ctx
from src.mcp_server.tools.soc.threat_intel.gdn_guardiankey._client import GDNClient, GDNError, OBJREFS
from src.shared.cache.decorator import cached
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# Cache TTL for dashboard query and usage results (seconds)
_CACHE_TTL_SECONDS = 300  # 5 minutes

# Human-readable descriptions for each available report
_REPORT_DESCRIPTIONS: dict[str, str] = {
    "top_users": "Top users ranked by total authentication event count",
    "top_clientips": "Top client IP addresses ranked by event count",
    "events": (
        "Detailed authentication event listing with timestamps, users, "
        "IPs, risk scores, and responses"
    ),
    "users": "User listing with activity summary per user (event counts, distinct IPs, cities)",
    "top_clientips_users": (
        "Top client IPs with distinct user counts — "
        "useful for detecting shared/suspicious origins"
    ),
    "top_users_cities": (
        "Top users ranked by number of distinct cities accessed from — "
        "useful for detecting impossible travel"
    ),
    "areas_risk_treatment_in_time": (
        "Risk treatment timeline: accepted/notified/blocked risk scores over time (series data)"
    ),
    "top_users_risk": "Top users ranked by cumulative risk score",
    "pie_event_responses": (
        "Event response distribution: count of accepted, soft_notify, "
        "hard_notify, and blocked events"
    ),
    "bars_events_in_time": "Authentication event volume over time (time series bar chart data)",
    "table_top_countries": "Top countries by authentication event count",
    "table_top_cities": "Top cities by authentication event count",
    "table_top_threats": "Top threat categories detected by gSageKey ranked by event count",
    "table_messagelog_events": "Message log events, such as alert email sending to users",
}


_TOOL_NAME = "gdn_guardiankey"


# ── Cached API helpers ─────────────────────────────────────────────────────
# The per-org cache is enforced by the decorator's scope="org" (it automatically
# includes the Guardian `org_id` in the final key). The logical key_fn below
# further discriminates by GDN tenant id + report + filters. ``user_id`` is
# intentionally NOT part of the key — results are shared across all users of
# the same org.


@cached(
    ttl=_CACHE_TTL_SECONDS,
    scope="org",
    key_fn=lambda *, gdn_org_id, report, filters, **_: (
        f"gdn:query:{gdn_org_id}:{report}:"
        + json.dumps(filters or {}, sort_keys=True)
    ),
    logical_name=_TOOL_NAME,
)
async def _fetch_gdn_query(
    *,
    url: str,
    api_key: str,
    gdn_org_id: str,
    report: str,
    filters: dict,
    state: dict,
    org_id: uuid.UUID,  # noqa: ARG001 — consumed by @cached (scope="org")
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> dict:
    """Fetch a GDN dashboard report. Only invoked on cache miss."""
    state["fetched"] = True
    async with GDNClient(url=url, api_key=api_key) as client:
        data = await client.get_dashboard_object_data(
            gdn_org_id, report, filters or None
        )
    return dict(data)


@cached(
    ttl=_CACHE_TTL_SECONDS,
    scope="org",
    key_fn=lambda *, gdn_org_id, **_: f"gdn:usage:{gdn_org_id}",
    logical_name=_TOOL_NAME,
)
async def _fetch_gdn_usage_summary(
    *,
    url: str,
    api_key: str,
    gdn_org_id: str,
    state: dict,
    org_id: uuid.UUID,  # noqa: ARG001 — consumed by @cached (scope="org")
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> dict[str, Any]:
    """Fetch GDN usage summary + per-authgroup breakdown. Only invoked on miss."""
    state["fetched"] = True
    async with GDNClient(url=url, api_key=api_key) as client:
        summary = await client.get_usage_summary(gdn_org_id)
        by_group = await client.get_usage_by_authgroup(gdn_org_id)

    return {
        "events": summary.get("events", 0),
        "users": summary.get("users", 0),
        "authgroups": [
            {
                "org_id": row[0] if len(row) > 0 else None,
                "authgroup_id": row[1] if len(row) > 1 else None,
                "name": row[2] if len(row) > 2 else None,
                "users": row[3] if len(row) > 3 else None,
                "events": row[4] if len(row) > 4 else None,
            }
            for row in by_group
            if isinstance(row, (list, tuple))
        ],
    }


class GdnGuardianKeyTool(BaseTool):
    """Query GuardianKey GDN analytics API.

    Retrieves dashboard report objects (events, top users, risk charts, etc.)
    and usage summaries from the GuardianKey GDN platform.  Results are cached
    in GSageToolCache (ORG scope, 5-minute TTL) to avoid redundant API calls.
    When requested, prefer to show all provided by the tool to the user 
    to avoid hiding potentially useful information, but include the report name and applied filters in the response for context.

    Permission: ``gdn:read``
    """

    name: ClassVar[str] = "gdn_guardiankey"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Query GuardianKey GDN analytics API for authentication events and risk-scoring data"
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["gdn:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["query", "list_reports", "usage_summary"],
                "default": "query",
                "description": (
                    "Action to perform. "
                    "'query' (default): retrieve a specific dashboard report with optional filters. "
                    "'list_reports': list all available report names and their descriptions — "
                    "no API call required. "
                    "'usage_summary': return total event/user counts and per-authgroup breakdown."
                ),
            },
            "report": {
                "type": "string",
                "enum": list(OBJREFS.keys()),
                "description": (
                    "Report to query (required for action='query'). "
                    "Available reports: "
                    "top_users (top users by event count), "
                    "top_clientips (top IPs by event count), "
                    "events (detailed event list), "
                    "users (user activity summary), "
                    "top_clientips_users (IPs with distinct user counts), "
                    "top_users_cities (users by distinct cities — impossible travel detection), "
                    "areas_risk_treatment_in_time (risk timeline series), "
                    "top_users_risk (users by cumulative risk score), "
                    "pie_event_responses (response distribution), "
                    "bars_events_in_time (event volume over time), "
                    "table_top_countries (top countries), "
                    "table_top_cities (top cities), "
                    "table_top_threats (top threat categories), "
                    "table_messagelog_events (WAP message log events)."
                ),
            },
            "days_ago": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "default": 7,
                "description": (
                    "Number of days back from now to use as the time range start. "
                    "Default: 7. Ignored if time_begin / time_end are provided."
                ),
            },
            "time_begin": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$",
                "description": (
                    "Explicit time range start in ISO 8601 format (YYYY-MM-DDTHH:MM). "
                    "When set, takes priority over days_ago."
                ),
            },
            "time_end": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$",
                "description": (
                    "Explicit time range end in ISO 8601 format (YYYY-MM-DDTHH:MM). "
                    "When set, takes priority over days_ago."
                ),
            },
            "username": {
                "type": "string",
                "description": "Filter results by username.",
            },
            "client_ip": {
                "type": "string",
                "description": "Filter results by client IP address.",
            },
            "login_failed": {
                "type": "boolean",
                "description": (
                    "Filter by login outcome: true = failed logins only, "
                    "false = successful logins only."
                ),
            },
            "country": {
                "type": "string",
                "description": "Filter results by country name.",
            },
            "response": {
                "type": "string",
                "enum": ["accepted", "hard_notify", "soft_notify", "blocked"],
                "description": "Filter results by gSageKey treatment response.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "GDN API base URL. The /api/v1 suffix is appended automatically "
                    "if absent. Overrides TOOL_GDN_GUARDIANKEY__URL env var."
                ),
            },
            "api_key": {
                "type": "string",
                "sensitive": True,
                "description": (
                    "GDN API key sent in the X-API-Key header. "
                    "Overrides TOOL_GDN_GUARDIANKEY__API_KEY env var."
                ),
            },
            "gdn_org_id": {
                "type": "string",
                "description": (
                    "Organisation ID in the GDN platform (hex string). "
                    "Overrides TOOL_GDN_GUARDIANKEY__GDN_ORG_ID env var."
                ),
            },
        },
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {"url": "", "api_key": "", "gdn_org_id": ""}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── entry point ───────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action: str = params.get("action") or "query"

        if action == "list_reports":
            return self._execute_list_reports(t0)

        # Resolve config — DB config / TOOL_GDN_GUARDIANKEY__* env vars
        url = (config.get("url") or "").strip()
        api_key = (config.get("api_key") or "").strip()
        gdn_org_id = (config.get("gdn_org_id") or "").strip()

        if not url:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "CONFIG_ERROR",
                "GDN base URL is not configured. Set 'url' in the tool config "
                "(TOOL_GDN_GUARDIANKEY__URL or GSageToolConfig).",
                retryable=False,
                execution_time_ms=elapsed,
            )
        if not api_key:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "CONFIG_ERROR",
                "GDN API key is not configured. Set 'api_key' in the tool config "
                "(TOOL_GDN_GUARDIANKEY__API_KEY or GSageToolConfig).",
                retryable=False,
                execution_time_ms=elapsed,
            )
        if not gdn_org_id and action in ("query", "usage_summary"):
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "CONFIG_ERROR",
                "GDN organisation ID is not configured. Set 'gdn_org_id' in the tool config "
                "(TOOL_GDN_GUARDIANKEY__GDN_ORG_ID or GSageToolConfig).",
                retryable=False,
                execution_time_ms=elapsed,
            )

        if action == "query":
            return await self._execute_query(
                agent_context, params, url, api_key, gdn_org_id, t0
            )
        if action == "usage_summary":
            return await self._execute_usage_summary(
                agent_context, url, api_key, gdn_org_id, t0
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._failure(
            "INVALID_ACTION",
            f"Unknown action '{action}'. Valid values: 'query', 'list_reports', 'usage_summary'.",
            retryable=False,
            execution_time_ms=elapsed,
        )

    # ── list_reports ──────────────────────────────────────────────────────

    def _execute_list_reports(self, t0: float) -> ToolResult:
        """Return static list of all available reports — no API call."""
        reports = [
            {
                "name": name,
                "description": _REPORT_DESCRIPTIONS[name],
                "api_path": OBJREFS[name],
            }
            for name in OBJREFS
        ]
        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {"reports": reports, "total": len(reports)},
            execution_time_ms=elapsed,
        )

    # ── query ─────────────────────────────────────────────────────────────

    async def _execute_query(
        self,
        agent_context: AgentContext,
        params: dict,
        url: str,
        api_key: str,
        gdn_org_id: str,
        t0: float,
    ) -> ToolResult:
        report: Optional[str] = params.get("report")
        if not report:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "MISSING_PARAM",
                "Parameter 'report' is required for action='query'. "
                "Use action='list_reports' to see available report names.",
                retryable=False,
                execution_time_ms=elapsed,
            )
        if report not in OBJREFS:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAM",
                f"Unknown report '{report}'. "
                f"Valid reports: {', '.join(OBJREFS.keys())}.",
                retryable=False,
                execution_time_ms=elapsed,
            )

        # Build filter dict
        filters = GDNClient.build_filter(
            days_ago=params.get("days_ago") or 7,
            time_begin=params.get("time_begin"),
            time_end=params.get("time_end"),
            username=params.get("username"),
            client_ip=params.get("client_ip"),
            login_failed=params.get("login_failed"),
            country=params.get("country"),
            response=params.get("response"),
        )
        # Explicit time_begin/time_end override days_ago (handled in build_filter, but
        # when both explicit and days_ago are given, explicit already wins)
        if params.get("time_begin") or params.get("time_end"):
            # Rebuild without days_ago so we don't override the explicit values
            filters = GDNClient.build_filter(
                days_ago=None,
                time_begin=params.get("time_begin"),
                time_end=params.get("time_end"),
                username=params.get("username"),
                client_ip=params.get("client_ip"),
                login_failed=params.get("login_failed"),
                country=params.get("country"),
                response=params.get("response"),
            )

        cache_key_state: dict = {"fetched": False}
        session = _tool_session_ctx.get()

        # Cache miss handling and fetch are performed inside the @cached helper.
        # The `state` dict lets us tell whether we hit the cache or called the API.
        try:
            if session is not None:
                data = await _fetch_gdn_query(
                    url=url,
                    api_key=api_key,
                    gdn_org_id=gdn_org_id,
                    report=report,
                    filters=filters,
                    state=cache_key_state,
                    org_id=agent_context.org_id,
                    session=session,
                )
            else:
                cache_key_state["fetched"] = True
                async with GDNClient(url=url, api_key=api_key) as client:
                    raw = await client.get_dashboard_object_data(
                        gdn_org_id, report, filters or None
                    )
                    data = dict(raw)
        except GDNError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.warning("gdn_guardiankey query error report=%s: %s", report, exc)
            retryable = exc.status_code in (0, 429, 500, 502, 503, 504)
            return self._failure(
                "API_ERROR",
                str(exc),
                retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.exception("gdn_guardiankey query unexpected error report=%s", report)
            return self._failure(
                "UNEXPECTED_ERROR",
                f"Unexpected error: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        result_data = dict(data)
        result_data["_cache"] = "miss" if cache_key_state["fetched"] else "hit"
        result_data["_report"] = report
        result_data["_filters"] = filters
        return self._success(result_data, execution_time_ms=elapsed)

    # ── usage_summary ─────────────────────────────────────────────────────

    async def _execute_usage_summary(
        self,
        agent_context: AgentContext,
        url: str,
        api_key: str,
        gdn_org_id: str,
        t0: float,
    ) -> ToolResult:
        cache_key_state: dict = {"fetched": False}
        session = _tool_session_ctx.get()

        try:
            if session is not None:
                data = await _fetch_gdn_usage_summary(
                    url=url,
                    api_key=api_key,
                    gdn_org_id=gdn_org_id,
                    state=cache_key_state,
                    org_id=agent_context.org_id,
                    session=session,
                )
            else:
                cache_key_state["fetched"] = True
                async with GDNClient(url=url, api_key=api_key) as client:
                    summary = await client.get_usage_summary(gdn_org_id)
                    by_group = await client.get_usage_by_authgroup(gdn_org_id)
                data = {
                    "events": summary.get("events", 0),
                    "users": summary.get("users", 0),
                    "authgroups": [
                        {
                            "org_id": row[0] if len(row) > 0 else None,
                            "authgroup_id": row[1] if len(row) > 1 else None,
                            "name": row[2] if len(row) > 2 else None,
                            "users": row[3] if len(row) > 3 else None,
                            "events": row[4] if len(row) > 4 else None,
                        }
                        for row in by_group
                        if isinstance(row, (list, tuple))
                    ],
                }
        except GDNError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.warning("gdn_guardiankey usage_summary error org=%s: %s", gdn_org_id, exc)
            retryable = exc.status_code in (0, 429, 500, 502, 503, 504)
            return self._failure(
                "API_ERROR",
                str(exc),
                retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.exception("gdn_guardiankey usage_summary unexpected error org=%s", gdn_org_id)
            return self._failure(
                "UNEXPECTED_ERROR",
                f"Unexpected error: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        result_data = dict(data)
        result_data["_cache"] = "miss" if cache_key_state["fetched"] else "hit"
        return self._success(result_data, execution_time_ms=elapsed)
