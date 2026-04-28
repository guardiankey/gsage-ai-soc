"""gSage AI — GLPI Get Group Members tool.

Returns the members of a GLPI group in a single call, including key user
fields (login, realname, firstname, email, phone, last_login). Optionally
includes members of sub-groups recursively (based on the parent group's
``completename`` prefix).

Without this tool, the equivalent workflow requires:

* 1× ``glpi_search`` (User with ``field=13 equals group_id``) — returns just id+login
* N× ``glpi_get_item`` (User) — to pull the remaining fields

This tool collapses that into one round-trip with the right ``forcedisplay``
fields, removing the N+1 problem from team-dashboard / on-call listings.

Required permission: ``glpi:read``
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# GLPI searchOption field IDs for User (standard GLPI 10.x layout).
_USER_FIELD_ID = 2
_USER_FIELD_LOGIN = 1
_USER_FIELD_REALNAME = 9
_USER_FIELD_FIRSTNAME = 10
_USER_FIELD_EMAIL = 34
_USER_FIELD_PHONE = 6
_USER_FIELD_MOBILE = 11
_USER_FIELD_LAST_LOGIN = 30
_USER_FIELD_IS_ACTIVE = 8
_USER_FIELD_GROUP = 13  # Group (via Group_User)

# searchOption field IDs for Group used to discover sub-groups by completename.
_GROUP_FIELD_ID = 2
_GROUP_FIELD_COMPLETENAME = 16
_GROUP_FIELD_NAME = 1

_USER_FORCEDISPLAY = [
    _USER_FIELD_ID,
    _USER_FIELD_LOGIN,
    _USER_FIELD_REALNAME,
    _USER_FIELD_FIRSTNAME,
    _USER_FIELD_EMAIL,
    _USER_FIELD_PHONE,
    _USER_FIELD_MOBILE,
    _USER_FIELD_LAST_LOGIN,
    _USER_FIELD_IS_ACTIVE,
]

# Map raw search row keys (always strings on GLPI) to friendly attribute names.
_USER_FIELD_LABELS: dict[int, str] = {
    _USER_FIELD_ID: "id",
    _USER_FIELD_LOGIN: "login",
    _USER_FIELD_REALNAME: "realname",
    _USER_FIELD_FIRSTNAME: "firstname",
    _USER_FIELD_EMAIL: "email",
    _USER_FIELD_PHONE: "phone",
    _USER_FIELD_MOBILE: "mobile",
    _USER_FIELD_LAST_LOGIN: "last_login",
    _USER_FIELD_IS_ACTIVE: "is_active",
}

_MAX_MEMBERS_HARD_LIMIT = 200


def _normalise_user_row(row: dict) -> dict[str, Any]:
    """Map a GLPI search row (keys are field IDs as strings) to a flat dict."""
    out: dict[str, Any] = {}
    for fid, label in _USER_FIELD_LABELS.items():
        # GLPI may return either int keys or string keys — try both.
        out[label] = row.get(str(fid), row.get(fid))
    # Build a friendly display_name from realname / firstname when available.
    realname = (out.get("realname") or "").strip()
    firstname = (out.get("firstname") or "").strip()
    if firstname or realname:
        out["display_name"] = f"{firstname} {realname}".strip()
    else:
        out["display_name"] = out.get("login")
    return out


class GlpiGetGroupMembersTool(BaseTool):
    """List the members of a GLPI group in a single call.

    **What it returns**

    For each user belonging to the group:

    - ``id``, ``login``, ``display_name``
    - ``realname``, ``firstname``
    - ``email``, ``phone``, ``mobile``
    - ``last_login``, ``is_active``

    **Use cases**

    - ``"who is on call in the GNTI on-call group?"``
      → ``group_id=329``
    - ``"all members of the SOC group and its sub-teams"``
      → ``group_id=339, include_subgroups=true``

    **Why a dedicated tool**

    GLPI's ``getItem`` for ``Group`` does **not** include the member list,
    and ``search/User`` only returns id + login by default — a separate
    ``getItem`` is needed for each user. This tool issues a single
    ``search/User`` with the right ``forcedisplay`` to fetch every relevant
    field in one round-trip.

    **Sub-group expansion**

    When ``include_subgroups=true``, this tool first looks up the parent
    group's ``completename`` and OR-searches for users belonging to any
    group whose ``completename`` starts with that prefix. This relies on
    the ``> `` separator GLPI uses for nested group names.

    Permission: ``glpi:read``
    """

    name: ClassVar[str] = "glpi_get_group_members"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "List members of a GLPI group (with optional recursive sub-group expansion)"
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["glpi:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["group_id"],
        "properties": {
            "group_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Numeric GLPI group ID.",
            },
            "include_subgroups": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, also include members of all sub-groups under the "
                    "given group (matched by 'completename' prefix). When false "
                    "(default), only direct members of this exact group are returned."
                ),
            },
            "active_only": {
                "type": "boolean",
                "default": True,
                "description": (
                    "When true (default), exclude users whose 'is_active' flag is 0. "
                    "Set to false to include disabled / archived users."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_MEMBERS_HARD_LIMIT,
                "default": 100,
                "description": "Maximum number of members to return (default: 100, max: 200).",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "GLPI REST API base URL (overrides TOOL_GLPI_GET_GROUP_MEMBERS__URL env var).",
            },
            "user_token": {
                "type": "string",
                "description": "GLPI user token (overrides TOOL_GLPI_GET_GROUP_MEMBERS__USER_TOKEN env var).",
            },
            "app_token": {
                "type": "string",
                "description": "GLPI application token (overrides TOOL_GLPI_GET_GROUP_MEMBERS__APP_TOKEN env var).",
            },
        },
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {"url": "", "user_token": "", "app_token": ""}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()

        group_id: int = int(params["group_id"])
        include_subgroups: bool = params.get("include_subgroups", False)
        active_only: bool = params.get("active_only", True)
        max_results: int = min(int(params.get("max_results", 100)), _MAX_MEMBERS_HARD_LIMIT)

        log.info(
            "glpi_get_group_members: group_id=%d include_subgroups=%s active_only=%s",
            group_id, include_subgroups, active_only,
        )

        try:
            async with GLPIClient(
                url=config.get("url") or None,
                user_token=config.get("user_token") or None,
                app_token=config.get("app_token") or None,
            ) as client:
                # Resolve group IDs to query (parent + optional sub-groups).
                group_ids: list[int] = [group_id]
                parent_completename: Optional[str] = None
                if include_subgroups:
                    parent_completename = await _fetch_group_completename(client, group_id)
                    if parent_completename:
                        sub_ids = await _fetch_subgroup_ids(client, parent_completename, group_id)
                        group_ids.extend(sub_ids)
                    else:
                        log.warning(
                            "glpi_get_group_members: could not resolve completename for group_id=%d; "
                            "include_subgroups will be ignored",
                            group_id,
                        )

                # Build OR group on field 13 (groups) for every group id we want to match.
                criteria: list[dict] = []
                for i, gid in enumerate(group_ids):
                    entry: dict = {
                        "field": _USER_FIELD_GROUP,
                        "searchtype": "equals",
                        "value": str(gid),
                    }
                    if i > 0:
                        entry["link"] = "OR"
                    criteria.append(entry)

                if active_only:
                    criteria.append({
                        "field": _USER_FIELD_IS_ACTIVE,
                        "searchtype": "equals",
                        "value": "1",
                        "link": "AND",
                    })

                range_str = f"0-{max_results - 1}"
                result = await client.search_items(
                    "User",
                    criteria,
                    range=range_str,
                    sort=_USER_FIELD_LOGIN,
                    order="ASC",
                    forcedisplay=_USER_FORCEDISPLAY,
                )

        except GLPIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.glpi_error or "GLPI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("glpi_get_group_members: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)

        rows = result.get("data") or []
        members = [_normalise_user_row(row) for row in rows]
        total = result.get("totalcount", len(members))

        return self._success(
            data={
                "summary": {
                    "group_id": group_id,
                    "include_subgroups": include_subgroups,
                    "queried_group_ids": group_ids,
                    "parent_completename": parent_completename,
                    "active_only": active_only,
                    "total_count": total,
                    "returned_count": len(members),
                },
                "members": members,
            },
            execution_time_ms=elapsed,
        )


# ── Module-level helpers ───────────────────────────────────────────────────


async def _fetch_group_completename(client: GLPIClient, group_id: int) -> Optional[str]:
    """Return the ``completename`` of a group (e.g. ``"GNTI > SOC > L1"``)."""
    try:
        item = await client.get_item("Group", group_id, expand_dropdowns=False)
    except GLPIError as exc:
        log.warning("glpi_get_group_members: failed to fetch group %d: %s", group_id, exc)
        return None
    if not isinstance(item, dict):
        return None
    completename = item.get("completename") or item.get("name")
    if isinstance(completename, str) and completename.strip():
        return completename.strip()
    return None


async def _fetch_subgroup_ids(
    client: GLPIClient,
    parent_completename: str,
    parent_id: int,
) -> list[int]:
    """Find all sub-group IDs whose ``completename`` starts with ``parent_completename + ' > '``.

    GLPI uses ``" > "`` as the hierarchy separator in completename. We use the
    ``contains`` searchtype because GLPI does not expose a native "starts with"
    operator and then filter client-side to keep only true descendants.
    """
    needle = f"{parent_completename} > "
    try:
        result = await client.search_items(
            "Group",
            [{"field": _GROUP_FIELD_COMPLETENAME, "searchtype": "contains", "value": needle}],
            range="0-199",
            forcedisplay=[_GROUP_FIELD_ID, _GROUP_FIELD_COMPLETENAME],
        )
    except GLPIError as exc:
        log.warning(
            "glpi_get_group_members: sub-group lookup failed for '%s': %s",
            parent_completename, exc,
        )
        return []

    rows = result.get("data") or []
    ids: list[int] = []
    for row in rows:
        completename = row.get(str(_GROUP_FIELD_COMPLETENAME)) or row.get(_GROUP_FIELD_COMPLETENAME)
        gid_raw = row.get(str(_GROUP_FIELD_ID)) or row.get(_GROUP_FIELD_ID)
        if not isinstance(completename, str) or not completename.startswith(needle):
            # contains may match groups that include the needle elsewhere; filter strictly.
            continue
        try:
            gid = int(gid_raw)
        except (TypeError, ValueError):
            continue
        if gid != parent_id:
            ids.append(gid)
    return ids
