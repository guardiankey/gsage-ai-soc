"""gSage AI — ad_read tool (Active Directory read-only).

Exposes six read-only operations against AD:

* ``list_users``     — paginated list of users under a base OU.
* ``list_groups``    — paginated list of groups under a base OU.
* ``list_ous``       — paginated list of OUs under a base DN.
* ``get_user``       — fetch a single user by DN or sAMAccountName.
* ``get_group``      — fetch a single group by DN or CN.
* ``audit_accounts`` — security-focused multi-query summary (stale,
  locked, never-logged-in, etc.).

All calls are performed over LDAP(S) by :class:`AdClient` and never
contact the Windows jump host.

Permission: ``ad:read``.  No approval required.
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.result_export import build_agent_payload
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

    Dispatches on ``params["action"]`` to one of six query methods.
    Returns JSON-safe dicts — dates are surfaced as ISO strings, binary
    attributes are omitted by design.

    Tabular results (list_* and audit_accounts with include_items) are
    capped at 100 inline rows; the full result is auto-exported as a CSV
    artifact when the row count exceeds the cap.

    Permission: ``ad:read``.
    """

    name: ClassVar[str] = "ad_read"
    config_namespace: ClassVar[str] = "active_directory"
    version: ClassVar[str] = "1.1.0"
    summary: ClassVar[str] = (
        "Read-only Active Directory queries: list/search users, groups, "
        "and OUs; fetch single user or group by DN or name; "
        "run security audit (stale accounts, locked out, never logged in)."
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
        export_csv = bool(params.get("export_csv", False))

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
                        password_changed_within_days=params.get("password_changed_within_days"),
                        password_changed_older_than_days=params.get("password_changed_older_than_days"),
                        last_logon_within_days=params.get("last_logon_within_days"),
                        last_logon_older_than_days=params.get("last_logon_older_than_days"),
                    )
                    data = await self._apply_csv_export(
                        data, export_csv, action, agent_context
                    )

                elif action == "list_groups":
                    data = await client.list_groups(
                        ou=params.get("ou"),
                        name_pattern=params.get("name_pattern"),
                        limit=int(params.get("limit") or 100),
                        offset=int(params.get("offset") or 0),
                    )
                    data = await self._apply_csv_export(
                        data, export_csv, action, agent_context
                    )

                elif action == "list_ous":
                    data = await client.list_ous(
                        base_dn=params.get("ou"),
                        mode=params.get("mode") or "flat",
                        limit=int(params.get("limit") or 200),
                        offset=int(params.get("offset") or 0),
                    )
                    data = await self._apply_csv_export(
                        data, export_csv, action, agent_context
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

                elif action == "audit_accounts":
                    categories: list[str] = params.get("audit_categories") or ["all"]
                    data = await client.audit_accounts(
                        ou=params.get("ou"),
                        categories=categories,
                        stale_days=int(params.get("stale_days") or 90),
                        password_change_days=int(params.get("password_change_days") or 30),
                        include_items=bool(params.get("include_items", False)),
                    )
                    data = await self._apply_audit_csv_export(
                        data, export_csv, action, agent_context
                    )

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

    # ── CSV export helpers ────────────────────────────────────────────

    async def _apply_csv_export(
        self,
        data: dict,
        export_csv: bool,
        action: str,
        agent_context: AgentContext,
    ) -> dict:
        """Apply CSV export to a list_* result, returning updated *data*."""
        rows: list[dict] = data.get("items") or []
        if not rows:
            return data

        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=False,
            filename_prefix=f"ad_read_{action}",
            agent_context=agent_context,
        )
        return {
            **data,
            "rows": agent_payload["rows_preview"],
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "artifacts": agent_payload["artifacts"],
            "agent_hint": agent_payload["agent_hint"],
        }

    async def _apply_audit_csv_export(
        self,
        data: dict,
        export_csv: bool,
        action: str,
        agent_context: AgentContext,
    ) -> dict:
        """Apply CSV export to an audit_accounts result.

        Each finding's items are replaced with a preview subset; the full
        combined list is persisted as a CSV artifact tagged with
        ``_audit_category``.
        """
        findings: dict = data.get("findings") or {}
        all_rows: list[dict] = []

        for cat_key, finding in findings.items():
            items: list[dict] = finding.get("items") or []
            if not items:
                continue
            for row in items:
                row["_audit_category"] = cat_key
            all_rows.extend(items)

        if not all_rows:
            return data

        agent_payload = await build_agent_payload(
            self,
            rows=all_rows,
            export_csv=export_csv,
            export_json=False,
            filename_prefix=f"ad_read_{action}",
            agent_context=agent_context,
        )

        # Partition preview rows back into per-category lists
        preview_rows: list[dict] = agent_payload["rows_preview"]
        preview_by_cat: dict[str, list[dict]] = {k: [] for k in findings}
        for row in preview_rows:
            cat = row.pop("_audit_category", None)
            if cat and cat in preview_by_cat:
                preview_by_cat[cat].append(row)

        for cat_key in findings:
            findings[cat_key]["items"] = preview_by_cat.get(cat_key, [])

        return {
            **data,
            "findings": findings,
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "artifacts": agent_payload["artifacts"],
            "agent_hint": agent_payload["agent_hint"],
        }
