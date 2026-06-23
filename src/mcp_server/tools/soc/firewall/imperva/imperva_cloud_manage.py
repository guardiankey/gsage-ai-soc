"""Approval-gated Imperva Cloud WAF configuration changes."""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.firewall.imperva._cloud_client import (
    IMPERVA_CLOUD_CONFIG_DEFAULTS, IMPERVA_CLOUD_CONFIG_SCHEMA, ImpervaCloudError,
    build_imperva_cloud_client,
)
from src.mcp_server.tools.soc.firewall.imperva._common import ParamError, optional_payload, require
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)
_ACTIONS = frozenset({"create_acl", "update_acl", "delete_acl", "add_whitelist", "remove_whitelist", "update_site_protection"})


class ImpervaCloudManageTool(BaseTool):
    name: ClassVar[str] = "imperva_cloud_manage"
    summary: ClassVar[str] = "Approval-gated Imperva Cloud WAF ACL, allowlist and site-protection management."
    category: ClassVar[str] = "firewall"
    config_namespace: ClassVar[str] = "imperva_cloud"
    permissions: ClassVar[list[str]] = ["imperva:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 45
    requires_approval: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    audit_field_mapping: ClassVar[dict] = {"action": "action", "target_entities": "site_id", "reason": "reason"}
    params_schema: ClassVar[dict] = {
        "type": "object", "required": ["action", "reason"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(_ACTIONS)}, "profile": {"type": "string"},
            "reason": {"type": "string", "minLength": 5}, "site_id": {"type": "string"},
            "acl_id": {"type": "string"}, "whitelist_id": {"type": "string"},
            "payload": {"type": "object", "description": "Action-specific Imperva rule, allowlist, or protection settings."},
        },
    }
    config_schema: ClassVar[Optional[dict]] = IMPERVA_CLOUD_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = IMPERVA_CLOUD_CONFIG_DEFAULTS

    async def execute(self, agent_context: AgentContext, params: dict, config: dict, state: dict) -> ToolResult:
        started, action = time.monotonic(), params.get("action")
        if action not in _ACTIONS:
            return self._failure("INVALID_PARAMS", f"action must be one of {sorted(_ACTIONS)}")
        try:
            site_id, payload = require(params, "site_id"), optional_payload(params)
            if action == "create_acl": method, path = "POST", "/security/acl"
            elif action == "update_acl": method, path = "PUT", f"/security/acl/{require(params, 'acl_id')}"
            elif action == "delete_acl": method, path = "DELETE", f"/security/acl/{require(params, 'acl_id')}"
            elif action == "add_whitelist": method, path = "POST", "/security/waf/white_list"
            elif action == "remove_whitelist": method, path = "DELETE", f"/security/waf/white_list/{require(params, 'whitelist_id')}"
            else: method, path = "PUT", f"/sites/{site_id}/security"
            async with build_imperva_cloud_client(config) as client:
                data = await client.request(method, path, params={"site_id": site_id}, payload=payload or None)
        except ParamError as exc:
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        except ImpervaCloudError as exc:
            return self._failure(exc.code, str(exc), retryable=exc.code in {"TIMEOUT", "CONNECTION_ERROR", "RATE_LIMITED"}, execution_time_ms=int((time.monotonic()-started)*1000))
        except Exception as exc:
            log.exception("imperva_cloud_manage failed")
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        return self._success({"action": action, "site_id": site_id, "result": data}, execution_time_ms=int((time.monotonic()-started)*1000))
