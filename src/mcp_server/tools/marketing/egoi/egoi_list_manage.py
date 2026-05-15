"""gSage AI — E-goi list management (create only).

Permission: ``egoi:write``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi._client import EgoiError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class EgoiListManageTool(BaseTool):
    """Create a new E-goi mailing list.

    Only the *create* action is exposed. List deletion is intentionally
    omitted because the E-goi API treats it as a destructive operation
    that cascades across contacts, segments and campaigns.

    Permission: ``egoi:write``
    """

    name: ClassVar[str] = "egoi_list_manage"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Manage E-goi mailing lists. Currently supports action='create'."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:write"]

    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False  # low risk: create-only

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.EGOI_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.EGOI_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "internal_name": "internal_name",
        "public_name": "public_name",
        "language": "language",
    }
    audit_output: ClassVar[bool] = True

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "internal_name"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create"],
                "description": "Only 'create' is supported.",
            },
            "internal_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "Internal (admin-facing) list name.",
            },
            "public_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "Public list name shown in subscribe forms.",
            },
            "language": {
                "type": "string",
                "minLength": 2,
                "maxLength": 5,
                "description": "ISO language code (e.g. 'EN', 'PT', 'pt_PT').",
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
        action = str(params.get("action") or "").strip()
        if action != "create":
            return self._failure(
                "VALIDATION_ERROR",
                f"Unsupported action '{action}'. Only 'create' is allowed.",
            )
        internal_name = str(params.get("internal_name") or "").strip()
        if not internal_name:
            return self._failure(
                "VALIDATION_ERROR", "'internal_name' is required"
            )

        body: dict = {"internal_name": internal_name}
        public_name = (params.get("public_name") or "").strip()
        if public_name:
            body["public_name"] = public_name
        language = (params.get("language") or "").strip()
        if language:
            body["lang"] = language

        try:
            async with Q.build_client(config) as client:
                payload = await client.create_list(body=body)
        except EgoiError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("egoi_list_manage: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        created = Q.normalize_list(payload) if isinstance(payload, dict) else {}
        return self._success(
            {
                "action": action,
                "list": created,
                "raw": payload if isinstance(payload, dict) else None,
            },
            execution_time_ms=elapsed,
        )
