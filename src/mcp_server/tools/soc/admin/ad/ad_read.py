"""gSage AI — ad_read tool (Active Directory read-only).

Exposes five read-only operations against AD:

* ``list_users``  — paginated list of users under a base OU.
* ``list_groups`` — paginated list of groups under a base OU.
* ``list_ous``    — paginated list of OUs under a base DN.
* ``get_user``    — fetch a single user by DN or sAMAccountName.
* ``get_group``   — fetch a single group by DN or CN.

All calls are performed over LDAP(S) by :class:`AdClient` and never
contact the Windows jump host.

Permission: ``ad:read``.  No approval required.
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.admin.ad._ad_client import AdLdapError, open_ad_client
from src.mcp_server.tools.soc.admin.ad._schemas import (
    AD_CONFIG_DEFAULTS,
    AD_CONFIG_SCHEMA,
    AD_READ_PARAMS_SCHEMA,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class AdReadTool(BaseTool):
    """Read-only queries against Active Directory via LDAP.

    Dispatches on ``params["action"]`` to one of five query methods.
    Returns JSON-safe dicts — dates are surfaced as ISO strings, binary
    attributes are omitted by design.

    Permission: ``ad:read``.
    """

    name: ClassVar[str] = "ad_read"
    config_namespace: ClassVar[str] = "active_directory"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only Active Directory queries: list/search users, groups, "
        "and OUs; fetch single user or group by DN or name."
    )
    category: ClassVar[str] = "admin"
    permissions: ClassVar[list[str]] = ["ad:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    # Per-org config (shared with ad_write via AD_CONFIG_SCHEMA).
    config_schema: ClassVar[Optional[dict]] = AD_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = AD_CONFIG_DEFAULTS
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "user_dn"}
    audit_output: ClassVar[bool] = True

    params_schema: ClassVar[Optional[dict]] = AD_READ_PARAMS_SCHEMA

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.perf_counter()
        action = params.get("action")

        if not action:
            return self._failure(
                "INVALID_PARAMS",
                "Missing required parameter 'action'.",
                retryable=False,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )

        try:
            async with open_ad_client(config) as client:
                if action == "list_users":
                    data = await client.list_users(
                        ou=params.get("ou"),
                        name_pattern=params.get("name_pattern"),
                        enabled=params.get("enabled"),
                        limit=int(params.get("limit") or 100),
                        offset=int(params.get("offset") or 0),
                    )
                elif action == "list_groups":
                    data = await client.list_groups(
                        ou=params.get("ou"),
                        name_pattern=params.get("name_pattern"),
                        limit=int(params.get("limit") or 100),
                        offset=int(params.get("offset") or 0),
                    )
                elif action == "list_ous":
                    data = await client.list_ous(
                        base_dn=params.get("ou"),
                        mode=params.get("mode") or "flat",
                        limit=int(params.get("limit") or 200),
                        offset=int(params.get("offset") or 0),
                    )
                elif action == "get_user":
                    user = await client.get_user(
                        user_dn=params.get("user_dn"),
                        sam_account_name=params.get("sam_account_name"),
                    )
                    if user is None:
                        return self._failure(
                            "NOT_FOUND",
                            "User not found.",
                            retryable=False,
                            execution_time_ms=int((time.perf_counter() - start) * 1000),
                        )
                    data = {"user": user}
                elif action == "get_group":
                    group = await client.get_group(
                        group_dn=params.get("group_dn"),
                        group_name=params.get("group_name"),
                    )
                    if group is None:
                        return self._failure(
                            "NOT_FOUND",
                            "Group not found.",
                            retryable=False,
                            execution_time_ms=int((time.perf_counter() - start) * 1000),
                        )
                    data = {"group": group}
                else:
                    return self._failure(
                        "INVALID_PARAMS",
                        f"Unsupported action '{action}'.",
                        retryable=False,
                        execution_time_ms=int((time.perf_counter() - start) * 1000),
                    )
        except AdLdapError as exc:
            log.info("ad_read %s failed: %s (%s)", action, exc.code, exc.message)
            retryable = exc.code in {"LDAP_CONNECT_FAILED"}
            return self._failure(
                exc.code,
                exc.message,
                retryable=retryable,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as exc:
            log.exception("ad_read %s raised unexpected error", action)
            return self._failure(
                "UNEXPECTED",
                f"Unexpected error: {exc}",
                retryable=True,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )

        return self._success(
            data={"action": action, **data},
            execution_time_ms=int((time.perf_counter() - start) * 1000),
        )
