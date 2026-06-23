"""gSage AI — SentinelOne threats / detections tool (read-only).

Read-only views over SentinelOne threats and the hash blocklist:

- ``list_threats``   — Threats with filters (free-text ``query``,
                     ``mitigation_status``, ``incident_status``,
                     ``analyst_verdict``, ``resolved``, ``site_ids``).
- ``get_threat``      — One threat by ``threat_id`` (flattened detail).
- ``threat_notes``    — Analyst notes on a threat.
- ``list_blocklist``  — Hash blocklist (restrictions) entries.

Tabular results auto-export as CSV over 100 rows. Permission:
``sentinelone:read``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.sentinelone._client import (
    S1_CONFIG_DEFAULTS,
    S1_CONFIG_SCHEMA,
    SentinelOneClient,
    SentinelOneError,
    build_s1_client,
)
from src.mcp_server.tools.soc.edr.sentinelone import _views as V
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "list_threats",
    "get_threat",
    "threat_notes",
    "list_blocklist",
})

_DEFAULT_RESULTS = 100
_MAX_RESULTS = 1000


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if val in (None, ""):
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


class S1ThreatsTool(BaseTool):
    """Read-only SentinelOne threats and hash blocklist.

    Use one ``action`` per call. Tabular results auto-export as CSV when
    over 100 rows. Permission: ``sentinelone:read``.
    """

    name: ClassVar[str] = "s1_threats"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only SentinelOne threats: list/get detections, analyst notes, "
        "hash blocklist. Auto-CSV on >100 rows."
    )
    category: ClassVar[str] = "edr"
    config_namespace: ClassVar[str] = "sentinelone"
    permissions: ClassVar[list[str]] = ["sentinelone:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "threat_id",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which read-only query to run.",
            },
            "profile": {"type": "string"},
            "threat_id": {
                "type": "string",
                "description": "[get_threat, threat_notes] Threat ID.",
            },
            "query": {
                "type": "string",
                "description": "[list_threats] Free-text search across threats.",
            },
            "mitigation_status": {
                "type": "string",
                "enum": [
                    "mitigated", "active", "blocked", "suspicious",
                    "pending", "suspicious_resolved", "marked_as_benign",
                ],
                "description": "[list_threats] Filter by mitigation status.",
            },
            "incident_status": {
                "type": "string",
                "enum": ["unresolved", "in_progress", "resolved"],
                "description": "[list_threats] Filter by incident status.",
            },
            "analyst_verdict": {
                "type": "string",
                "enum": ["undefined", "true_positive", "false_positive", "suspicious"],
                "description": "[list_threats] Filter by analyst verdict.",
            },
            "resolved": {
                "type": "boolean",
                "description": "[list_threats] Filter resolved vs unresolved.",
            },
            "site_ids": {
                "type": "string",
                "description": (
                    "Comma-separated site IDs to scope the query. Defaults "
                    "to the profile's default_site_ids."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "description": (
                    f"Maximum items to return (default {_DEFAULT_RESULTS}, "
                    f"hard cap {_MAX_RESULTS})."
                ),
            },
            "export_csv": {"type": "boolean", "description": "Force CSV artifact."},
            "export_json": {"type": "boolean", "description": "Persist a JSON artifact."},
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = S1_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = S1_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ─────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = (params.get("action") or "").strip()
        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )
        max_results = min(
            int(params.get("max_results") or _DEFAULT_RESULTS), _MAX_RESULTS
        )
        try:
            async with build_s1_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, agent_context, max_results)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=elapsed)
        except SentinelOneError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED")
            return self._failure(
                exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed
            )
        except Exception as exc:
            log.exception("s1_threats(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(data={"action": action, **data}, execution_time_ms=elapsed)

    # ── Tabular helper ──────────────────────────────────────────────────────

    async def _tabular(
        self, agent_context: AgentContext, action: str, rows: list[dict], params: dict,
    ) -> dict:
        payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_{action}",
            agent_context=agent_context,
        )
        return {
            "rows_total": payload["rows_total"],
            "rows_overflow": payload["rows_overflow"],
            "rows_preview_limit": AGENT_PREVIEW_ROWS,
            "artifacts": payload["artifacts"],
            "agent_hint": payload["agent_hint"],
            "rows": payload["rows_preview"],
        }

    # ── Actions ──────────────────────────────────────────────────────────────

    async def _do_list_threats(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        q: dict = {}
        if (query := (params.get("query") or "").strip()):
            q["query"] = query
        if (ms := (params.get("mitigation_status") or "").strip()):
            q["mitigationStatuses"] = ms
        if (inc := (params.get("incident_status") or "").strip()):
            q["incidentStatuses"] = inc
        if (av := (params.get("analyst_verdict") or "").strip()):
            q["analystVerdicts"] = av
        if params.get("resolved") is not None:
            q["resolved"] = "true" if params.get("resolved") else "false"
        if (sites := client.resolve_site_ids(params)):
            q["siteIds"] = sites
        rows = await client.paginate("/threats", q, max_items=max_results)
        return await self._tabular(
            agent_context, "list_threats", [V.slim_threat(t) for t in rows], params
        )

    async def _do_get_threat(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        tid = _require(params, "threat_id")
        body = await client.get("/threats", {"ids": tid})
        row = V.first_or_none(body)
        if row is None:
            raise SentinelOneError(f"Threat {tid} not found.", code="NOT_FOUND")
        return {"threat": V.slim_threat(row)}

    async def _do_threat_notes(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        tid = _require(params, "threat_id")
        rows = await client.paginate(
            f"/threats/{tid}/notes", {}, max_items=max_results
        )
        return {
            "threat_id": tid,
            "note_count": len(rows),
            "notes": [V.slim_note(n) for n in rows][:max_results],
        }

    async def _do_list_blocklist(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        q: dict = {"type": "black_hash"}
        if (sites := client.resolve_site_ids(params)):
            q["siteIds"] = sites
        rows = await client.paginate("/restrictions", q, max_items=max_results)
        return await self._tabular(
            agent_context, "list_blocklist",
            [V.slim_blocklist_item(b) for b in rows], params,
        )


_ = Any
