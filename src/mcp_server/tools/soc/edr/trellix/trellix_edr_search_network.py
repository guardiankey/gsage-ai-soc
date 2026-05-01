"""gSage AI — Trellix EDR network flow hunt.

Convenience shortcut over the v1 NetworkFlow projection.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._artifacts import build_agent_payload
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class TrellixEdrSearchNetworkTool(BaseTool):
    """Hunt network flows across endpoints (NetworkFlow + Processes correlation).

    Examples:
        - ``remote_ip="203.0.113.5"`` → every host that talked to that IP.
        - ``remote_port=443`` + ``process_name="powershell"`` → PS hitting 443.

    Permission: ``edr:read``
    """

    name: ClassVar[str] = "trellix_edr_search_network"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search network flows on Trellix EDR endpoints by remote IP/port, "
        "process name or hostname"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 900
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    always_background: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.TRELLIX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.TRELLIX_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "remote_ip"}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "remote_ip": {
                "type": "string",
                "description": (
                    "IP that appears on either side of the flow. Internally "
                    "matched as (NetworkFlow.src_ip EQUALS X) OR "
                    "(NetworkFlow.dst_ip EQUALS X) so direction does not matter."
                ),
            },
            "remote_port": {
                "type": "integer",
                "minimum": 0,
                "maximum": 65535,
                "description": (
                    "TCP/UDP port that appears on either side of the flow "
                    "(matched against src_port and dst_port)."
                ),
            },
            "process_name": {
                "type": "string",
                "description": (
                    "Substring match on the originating process image name "
                    "(NetworkFlow.process CONTAINS)."
                ),
            },
            "hostname_contains": {
                "type": "string",
                "description": "Substring match on hostname.",
            },
            "direction": {
                "type": "string",
                "enum": ["in", "out"],
                "description": "Flow direction filter.",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all rows as a CSV file artifact. PREFER CSV "
                    "over JSON for tabular results. When the caller asks "
                    "to save/export/download without specifying a format, "
                    "set this to true."
                ),
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all rows as JSON. Only when the user "
                    "explicitly asks for JSON — otherwise use 'export_csv'."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()

        remote_ip = (params.get("remote_ip") or "").strip() or None
        remote_port = params.get("remote_port")
        process_name = (params.get("process_name") or "").strip() or None
        hostname_contains = (params.get("hostname_contains") or "").strip() or None
        direction = params.get("direction") or None

        if not any([remote_ip, remote_port is not None, process_name, hostname_contains, direction]):
            return self._failure(
                "INVALID_INPUT",
                "Provide at least one of: remote_ip, remote_port, process_name, "
                "hostname_contains, direction.",
            )

        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))

        payload = Q.build_network_payload(
            remote_ip=remote_ip,
            remote_port=int(remote_port) if remote_port is not None else None,
            process_name=process_name,
            hostname_contains=hostname_contains,
            direction=direction,
        )

        try:
            async with Q.build_client(config) as client:
                query_id, rows, meta, truncated = await Q.run_search_pipeline(
                    client,
                    api_version="v1",
                    payload=payload,
                    max_rows=max_rows,
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("trellix_edr_search_network: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        summary = Q.summarize(
            rows,
            group_by=[
                "HostInfo_hostname",
                "NetworkFlow_src_ip",
                "NetworkFlow_dst_ip",
                "NetworkFlow_dst_port",
                "NetworkFlow_process",
            ],
        )
        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=f"trellix_edr_network_{query_id}",
            agent_context=agent_context,
        )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "query_id": query_id,
                "api_version": "v1",
                "criteria": {
                    "remote_ip": remote_ip,
                    "remote_port": remote_port,
                    "process_name": process_name,
                    "hostname_contains": hostname_contains,
                    "direction": direction,
                },
                "total_count": meta.get("total_count", len(rows)),
                "total_hosts": meta.get("total_hosts", 0),
                "truncated": truncated,
                "artifacts": agent_payload["artifacts"],
                "rows_total": agent_payload["rows_total"],
                "rows_overflow": agent_payload["rows_overflow"],
                "agent_hint": agent_payload["agent_hint"],
                "rows_preview_limit": Q.AGENT_PREVIEW_ROWS,
                "summary": summary,
                "rows": agent_payload["rows_preview"],
            },
            execution_time_ms=elapsed,
        )
