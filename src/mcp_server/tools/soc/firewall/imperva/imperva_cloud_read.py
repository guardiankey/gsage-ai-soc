"""Read-only Imperva Cloud WAF inventory and protection inspection."""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.firewall.imperva._cloud_client import (
    IMPERVA_CLOUD_CONFIG_DEFAULTS, IMPERVA_CLOUD_CONFIG_SCHEMA, ImpervaCloudError,
    build_imperva_cloud_client,
)
from src.mcp_server.tools.soc.firewall.imperva._common import ParamError, compact, require
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)
_ACTIONS = frozenset({"list_sites", "get_site", "get_site_status", "list_acls", "list_waf_rules", "list_whitelists"})


class ImpervaCloudReadTool(BaseTool):
    name: ClassVar[str] = "imperva_cloud_read"
    summary: ClassVar[str] = "Inspect Imperva Cloud WAF sites, protection state, ACLs, WAF rules and allowlists."
    category: ClassVar[str] = "firewall"
    config_namespace: ClassVar[str] = "imperva_cloud"
    permissions: ClassVar[list[str]] = ["imperva:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 45
    requires_config: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    audit_field_mapping: ClassVar[dict] = {"action": "action", "target_entities": "site_id"}
    params_schema: ClassVar[dict] = {
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(_ACTIONS)},
            "profile": {"type": "string", "description": "Imperva Cloud configuration profile."},
            "site_id": {"type": "string", "description": "Required for get_site, get_site_status and site-scoped list actions."},
        },
    }
    config_schema: ClassVar[Optional[dict]] = IMPERVA_CLOUD_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = IMPERVA_CLOUD_CONFIG_DEFAULTS

    async def execute(self, agent_context: AgentContext, params: dict, config: dict, state: dict) -> ToolResult:
        started, action = time.monotonic(), params.get("action")
        if action not in _ACTIONS:
            return self._failure("INVALID_PARAMS", f"action must be one of {sorted(_ACTIONS)}")
        try:
            async with build_imperva_cloud_client(config) as client:
                paths = {
                    "list_sites": ("/sites", None), "get_site": ("/sites/{site_id}", "site_id"),
                    "get_site_status": ("/sites/status", "site_id"), "list_acls": ("/security/acl", "site_id"),
                    "list_waf_rules": ("/security/waf/rules", "site_id"), "list_whitelists": ("/security/waf/white_list", "site_id"),
                }
                path, scope = paths[action]
                site_id = require(params, "site_id") if scope else None
                if "{site_id}" in path:
                    path = path.format(site_id=site_id)
                data = await client.request("GET", path, params={"site_id": site_id} if scope and "{site_id}" not in paths[action][0] else None)
        except ParamError as exc:
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        except ImpervaCloudError as exc:
            return self._failure(exc.code, str(exc), retryable=exc.code in {"TIMEOUT", "CONNECTION_ERROR", "RATE_LIMITED"}, execution_time_ms=int((time.monotonic()-started)*1000))
        except Exception as exc:
            log.exception("imperva_cloud_read failed")
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=int((time.monotonic()-started)*1000))
        return self._success({"action": action, "result": compact(data)}, execution_time_ms=int((time.monotonic()-started)*1000))
