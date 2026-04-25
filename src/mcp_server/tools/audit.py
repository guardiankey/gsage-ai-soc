"""gSage AI — Tool execution audit logger (Elasticsearch)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.shared.elasticsearch.client import ElasticsearchClient
from src.shared.elasticsearch.redis_buffer import enqueue_for_es
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)


class ToolAuditLogger:
    """
    Logs all tool executions to Elasticsearch ``tool_audit_log`` index.

    Index fields (from PROMPT.md Phase 2 spec):
        @timestamp, org_id, user_id, trace_id, tool_name, tool_version,
        input_params, status, error_code, execution_time_ms, source,
        audit_context

    ``audit_context`` is a structured sub-document with optional business
    context supplied by the LLM agent (``_audit_context`` param) merged with
    fields extracted automatically from input params (``audit_field_mapping``).
    It is intended for **human review only** — never use it as a trusted
    source of truth for automated decisions.

    Security note:
        - Config values and secrets are NEVER included in input_params.
        - The caller is responsible for sanitizing input_params before logging.
    """

    def __init__(self, es_client: ElasticsearchClient) -> None:
        self.es = es_client

    async def log_execution(
        self,
        agent_context: AgentContext,
        tool_name: str,
        tool_version: str,
        input_params: dict,
        status: str,
        execution_time_ms: int,
        error_code: Optional[str] = None,
        error_details: Optional[str] = None,
        audit_context: Optional[dict] = None,
        output_data: Optional[dict] = None,
    ) -> None:
        """
        Write tool execution record to Elasticsearch.

        Args:
            agent_context: Request context (org, user, trace).
            tool_name: Tool identifier (e.g., "dns_lookup").
            tool_version: Semver string (e.g., "1.0.0").
            input_params: Sanitized tool input (NO secrets).
            status: "success" | "error" | "partial" | "permission_denied" | "rate_limited".
            execution_time_ms: Wall-clock duration.
            error_code: e.g., "TOOL_TIMEOUT", "CIRCUIT_OPEN", "PERMISSION_DENIED".
            error_details: Full exception traceback for debugging (optional).
            audit_context: Optional business-context dict for team-level audit.
                Fields: reason, ticket_id, severity, target_entities, notes.
                Populated by the LLM agent and/or auto-extracted from params.
                Treated as advisory — never as a source of truth for automation.
            output_data: Optional tool result data (opt-in per tool via
                ``audit_output = True``). Truncated to 4 KB if too large.
                Stored in ES as a non-indexed object (``enabled: false``).

        Note:
            Fire-and-forget. Failures are logged locally but never raise.
        """
        # Serialize and truncate output_data before building the doc.
        stored_output: Optional[dict] = None
        if output_data is not None:
            import json  # noqa: PLC0415
            try:
                raw = json.dumps(output_data, default=str)
                if len(raw) > 4096:
                    stored_output = {"_truncated": True, "_partial": raw[:4096]}
                else:
                    stored_output = output_data
            except Exception:
                stored_output = {"_error": "output serialisation failed"}

        doc = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "org_id": str(agent_context.org_id),
            "user_id": str(agent_context.user_id),
            "trace_id": str(agent_context.request_id),
            "tool_name": tool_name,
            "tool_version": tool_version,
            "input_params": input_params,
            "output_data": stored_output,
            "status": status,
            "error_code": error_code,
            "error_details": error_details,
            "execution_time_ms": execution_time_ms,
            "source": agent_context.source.value,
            "audit_context": audit_context or None,
        }

        # Enqueue for async bulk insert via Celery flush task
        enqueue_for_es("tool-audit-log", doc)
