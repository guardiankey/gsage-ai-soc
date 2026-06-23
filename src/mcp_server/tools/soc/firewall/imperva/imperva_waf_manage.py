"""Approval-gated policy administration for Imperva SecureSphere WAF 15.x."""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional
from urllib.parse import quote

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.firewall.imperva._common import ParamError, optional_payload, require
from src.mcp_server.tools.soc.firewall.imperva._waf_client import (
    IMPERVA_WAF_CONFIG_DEFAULTS, IMPERVA_WAF_CONFIG_SCHEMA, ImpervaWafError,
    build_imperva_waf_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)
_ACTIONS = frozenset({"create_policy", "update_policy", "delete_policy", "create_profile", "update_profile", "delete_profile", "create_rule", "update_rule", "delete_rule", "create_ip_group", "update_ip_group", "delete_ip_group", "associate_policy"})


class ImpervaWafManageTool(BaseTool):
    name: ClassVar[str] = "imperva_waf_manage"
    summary: ClassVar[str] = "Approval-gated SecureSphere policy, profile, rule, IP group and policy-association management."
    category: ClassVar[str] = "firewall"
    config_namespace: ClassVar[str] = "imperva_waf"
    permissions: ClassVar[list[str]] = ["imperva:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 60
    requires_approval: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    audit_field_mapping: ClassVar[dict] = {"action": "action", "target_entities": "name", "reason": "reason"}
    params_schema: ClassVar[dict] = {
        "type": "object", "required": ["action", "reason"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(_ACTIONS)}, "profile": {"type": "string"},
            "reason": {"type": "string", "minLength": 5}, "name": {"type": "string", "description": "Policy, profile, rule, or IP group name for update/delete."},
            "server_name": {"type": "string", "description": "Target server for associate_policy."},
            "policy_name": {"type": "string", "description": "Policy to associate for associate_policy."},
            "payload": {"type": "object", "description": "Action-specific SecureSphere object definition."},
        },
    }
    config_schema: ClassVar[Optional[dict]] = IMPERVA_WAF_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = IMPERVA_WAF_CONFIG_DEFAULTS

    async def execute(self, agent_context: AgentContext, params: dict, config: dict, state: dict) -> ToolResult:
        started, action = time.monotonic(), params.get("action")
        if action not in _ACTIONS:
            return self._failure("INVALID_PARAMS", f"action must be one of {sorted(_ACTIONS)}")
        try:
            payload = optional_payload(params)
            if action == "associate_policy":
                method, path = "PUT", f"/servers/{quote(require(params, 'server_name'), safe='')}/policy"
                payload = {**payload, "policy_name": require(params, "policy_name")}
            else:
                noun = action.split("_", 1)[1]
                collection = {"policy": "policies", "profile": "profiles", "rule": "rules", "ip_group": "ip-groups"}[noun]
                verb = action.split("_", 1)[0]
                if verb == "create":
                    method, path = "POST", f"/{collection}"
                else:
                    name = quote(require(params, "name"), safe="")
                    method, path = ("PUT" if verb == "update" else "DELETE"), f"/{collection}/{name}"
            async with build_imperva_waf_client(config) as client:
                data = await client.request(method, path, payload=payload or None)
        except ParamError as exc:
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        except ImpervaWafError as exc:
            return self._failure(exc.code, str(exc), retryable=exc.code in {"TIMEOUT", "CONNECTION_ERROR", "RATE_LIMITED"}, execution_time_ms=int((time.monotonic()-started)*1000))
        except Exception as exc:
            log.exception("imperva_waf_manage failed")
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        return self._success({"action": action, "result": data}, execution_time_ms=int((time.monotonic()-started)*1000))
