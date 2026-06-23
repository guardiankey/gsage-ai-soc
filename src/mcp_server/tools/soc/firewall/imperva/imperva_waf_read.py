"""Read-only inventory for Imperva SecureSphere WAF 15.x."""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.firewall.imperva._common import ParamError, compact, require
from src.mcp_server.tools.soc.firewall.imperva._waf_client import (
    IMPERVA_WAF_CONFIG_DEFAULTS, IMPERVA_WAF_CONFIG_SCHEMA, ImpervaWafError,
    build_imperva_waf_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)
_ACTIONS = frozenset({"list_policies", "get_policy", "list_profiles", "get_profile", "list_rules", "list_ip_groups", "list_servers"})


class ImpervaWafReadTool(BaseTool):
    name: ClassVar[str] = "imperva_waf_read"
    summary: ClassVar[str] = "Inspect Imperva SecureSphere policies, profiles, rules, IP groups and protected servers."
    category: ClassVar[str] = "firewall"
    config_namespace: ClassVar[str] = "imperva_waf"
    permissions: ClassVar[list[str]] = ["imperva:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 45
    requires_config: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    audit_field_mapping: ClassVar[dict] = {"action": "action", "target_entities": "name"}
    params_schema: ClassVar[dict] = {
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": {"action": {"type": "string", "enum": sorted(_ACTIONS)}, "profile": {"type": "string"}, "name": {"type": "string", "description": "Required for get_policy and get_profile."}},
    }
    config_schema: ClassVar[Optional[dict]] = IMPERVA_WAF_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = IMPERVA_WAF_CONFIG_DEFAULTS

    async def execute(self, agent_context: AgentContext, params: dict, config: dict, state: dict) -> ToolResult:
        started, action = time.monotonic(), params.get("action")
        if action not in _ACTIONS:
            return self._failure("INVALID_PARAMS", f"action must be one of {sorted(_ACTIONS)}")
        try:
            paths = {"list_policies": "/policies", "get_policy": "/policies/{name}", "list_profiles": "/profiles", "get_profile": "/profiles/{name}", "list_rules": "/rules", "list_ip_groups": "/ip-groups", "list_servers": "/servers"}
            path = paths[action]
            if "{name}" in path:
                path = path.format(name=require(params, "name"))
            async with build_imperva_waf_client(config) as client:
                data = await client.request("GET", path)
        except ParamError as exc:
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        except ImpervaWafError as exc:
            return self._failure(exc.code, str(exc), retryable=exc.code in {"TIMEOUT", "CONNECTION_ERROR", "RATE_LIMITED"}, execution_time_ms=int((time.monotonic()-started)*1000))
        except Exception as exc:
            log.exception("imperva_waf_read failed")
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        return self._success({"action": action, "result": compact(data)}, execution_time_ms=int((time.monotonic()-started)*1000))
