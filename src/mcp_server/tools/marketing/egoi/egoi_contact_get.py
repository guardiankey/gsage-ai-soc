"""gSage AI — Get a single E-goi contact by id.

Permission: ``egoi:read``
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


class EgoiContactGetTool(BaseTool):
    """Fetch a single E-goi contact by ``list_id`` + ``contact_id``.

    Returns the full normalised contact object plus the raw ``extra``
    fields sub-document for ad-hoc inspection.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_contact_get"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Fetch a single E-goi contact by list_id + contact_id. Use "
        "egoi_contact_search first to locate the ids."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.EGOI_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.EGOI_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["list_id", "contact_id"],
        "properties": {
            "list_id": {"type": "integer", "minimum": 1},
            "contact_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "E-goi contact identifier. Usually a 10-char hex "
                    "hash (e.g. 'a7c0458bb4'); pass it verbatim from "
                    "egoi_contact_search results."
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
        list_id = int(params["list_id"])
        contact_id = str(params["contact_id"]).strip()
        if not contact_id:
            return self._failure(
                "VALIDATION_ERROR", "contact_id must be a non-empty string"
            )

        try:
            async with Q.build_client(config) as client:
                payload = await client.get_contact(
                    list_id=list_id, contact_id=contact_id
                )
        except EgoiError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("egoi_contact_get: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        if not isinstance(payload, dict):
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "NOT_FOUND",
                f"Contact {contact_id} not found in list {list_id}",
                execution_time_ms=elapsed,
            )

        contact = Q.normalize_contact(payload)
        contact.setdefault("list_id", list_id)
        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "list_id": list_id,
                "contact_id": contact_id,
                "contact": contact,
                "raw_extra": payload.get("extra"),
            },
            execution_time_ms=elapsed,
        )
