"""gSage AI — Public Security Alerts tool.

Aggregates cybersecurity alerts from official Brazilian sources (CTIR, CISC,
CAIS).  Returns listing metadata only (title, date, link); the agent uses
``http_fetch`` to retrieve full alert content on demand.

Permission: ``threat_intel:public_alerts:read``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.threat_intel.public_alerts._base import SourceParser
from src.mcp_server.tools.soc.threat_intel.public_alerts._ctir import CTIRParser
from src.mcp_server.tools.soc.threat_intel.public_alerts._cisc import CISCParser
from src.mcp_server.tools.soc.threat_intel.public_alerts._cais import CAISParser
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_SOURCES: dict[str, type[SourceParser]] = {
    "ctir": CTIRParser,
    "cisc": CISCParser,
    "cais": CAISParser,
}

_ACTIONS = frozenset({"list_sources", "fetch_all", "fetch_source"})
_SOURCE_IDS = frozenset(_SOURCES.keys())
_DEFAULT_MAX_RESULTS = 10
_MAX_RESULTS_HARD_CAP = 100


class PublicAlertsTool(BaseTool):
    """Aggregate public cybersecurity alerts from Brazilian official sources.

    Actions
    -------
    - ``list_sources`` — return metadata for all supported sources.
    - ``fetch_all`` — fetch the latest alerts from ALL sources.
    - ``fetch_source`` — fetch alerts from a single source (use ``source`` param).

    Permission: ``threat_intel:public_alerts:read``.
    """

    name: ClassVar[str] = "public_alerts"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Fetch public cybersecurity alerts from Brazilian sources "
        "(CTIR, CISC, CAIS). Returns listing metadata: title, date, link. "
        "Use http_fetch to read full alert content."
    )
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["threat_intel:public_alerts:read"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = False
    requires_config: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which operation to perform.",
            },
            "source": {
                "type": "string",
                "enum": sorted(_SOURCE_IDS),
                "description": "Source ID (required for fetch_source).",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS_HARD_CAP,
                "description": (
                    f"Max alerts per source (default {_DEFAULT_MAX_RESULTS}, "
                    f"hard cap {_MAX_RESULTS_HARD_CAP})."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "description": "Bypass cache for this call.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ───────────────────────────────────────────────────────────

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
            int(params.get("max_results") or _DEFAULT_MAX_RESULTS),
            _MAX_RESULTS_HARD_CAP,
        )

        try:
            if action == "list_sources":
                data = self._do_list_sources()
            elif action == "fetch_all":
                data = await self._do_fetch_all(max_results)
            elif action == "fetch_source":
                data = await self._do_fetch_source(params, max_results)
            else:
                raise ValueError(f"Unknown action: {action}")
        except Exception as exc:
            log.exception("public_alerts(%s): error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(data=data, execution_time_ms=elapsed)

    # ── Actions ──────────────────────────────────────────────────────────

    def _do_list_sources(self) -> dict:
        """Return metadata for all supported sources."""
        sources = []
        for sid, parser_cls in _SOURCES.items():
            sources.append({
                "id": parser_cls.source_id,
                "name": parser_cls.source_name,
                "full_name": parser_cls.source_full_name,
                "url": parser_cls.list_url,
                "update_frequency": parser_cls.update_frequency,
            })
        return {"sources": sources}

    async def _do_fetch_all(self, max_results: int) -> dict:
        """Fetch alerts from all sources, returning consolidated results."""
        all_alerts: list[dict] = []
        errors: list[dict] = []

        for sid, parser_cls in _SOURCES.items():
            try:
                alerts = await parser_cls.fetch_and_parse(
                    max_results=max_results,
                )
                all_alerts.extend(alerts)
                log.info(
                    "public_alerts: %s returned %d alerts",
                    sid, len(alerts),
                )
            except Exception as exc:
                log.warning("public_alerts: %s failed: %s", sid, exc)
                errors.append({"source": sid, "error": str(exc)})

        # Sort all alerts by published_at descending
        all_alerts.sort(key=lambda a: a.get("published_at", ""), reverse=True)

        return {
            "alerts": all_alerts[:max_results * len(_SOURCES)],
            "total": len(all_alerts),
            "sources_queried": list(_SOURCES.keys()),
            "errors": errors or None,
        }

    async def _do_fetch_source(
        self, params: dict, max_results: int
    ) -> dict:
        """Fetch alerts from a single source."""
        source_id = (params.get("source") or "").strip()
        if source_id not in _SOURCE_IDS:
            raise ValueError(
                f"source must be one of {sorted(_SOURCE_IDS)}; "
                f"got {source_id!r}."
            )

        parser_cls = _SOURCES[source_id]
        alerts = await parser_cls.fetch_and_parse(max_results=max_results)
        return {
            "source": source_id,
            "alerts": alerts,
            "total": len(alerts),
        }
