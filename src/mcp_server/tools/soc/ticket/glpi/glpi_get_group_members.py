"""gSage AI — GLPI Get Group Members tool.

Returns the members of a GLPI group in a single call, including key user
fields (login, realname, firstname, email, phone, last_login). Optionally
includes members of sub-groups recursively (based on the parent group's
``completename`` prefix).

Implementation notes
--------------------
GLPI's ``search/User`` endpoint with ``forcedisplay`` returned the correct
``totalcount`` but empty field values for several joined/derived columns
(realname, email, …) on some GLPI 10.x installations. To avoid relying on
fragile ``searchOption`` IDs, this tool uses the more deterministic CRUD
endpoints:

* ``GET /Group/{id}/Group_User`` — returns ``users_id`` for every direct
  member of the group.
* ``GET /User/{users_id}`` (in parallel) — returns the canonical user
  record with stable JSON keys (``name``, ``realname``, ``firstname``,
  ``phone``, ``mobile``, ``last_login``, ``is_active``).
* ``GET /User/{users_id}/UserEmail`` — returns the user's email
  addresses (default one is preferred).

Required permission: ``glpi:read``
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# searchOption field IDs for Group used to discover sub-groups by completename.
_GROUP_FIELD_ID = 2
_GROUP_FIELD_COMPLETENAME = 16
_GROUP_FIELD_NAME = 1

_MAX_MEMBERS_HARD_LIMIT = 200
# Cap concurrent per-user GETs to avoid hammering GLPI.
_USER_FETCH_CONCURRENCY = 8
# Max members to enumerate from Group_User per group (before client-side trim).
_GROUP_USER_RANGE = "0-499"


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort conversion to int (GLPI often returns numeric fields as strings)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_member(item: dict, email: Optional[str], fallback_id: int) -> dict[str, Any]:
    """Map a GLPI ``User`` getItem response to the tool's flat output shape."""
    realname = (item.get("realname") or "").strip()
    firstname = (item.get("firstname") or "").strip()
    login = item.get("name")
    if firstname or realname:
        display_name = f"{firstname} {realname}".strip()
    else:
        display_name = login
    return {
        "id": _coerce_int(item.get("id")) or fallback_id,
        "login": login,
        "realname": realname or None,
        "firstname": firstname or None,
        "email": email,
        "phone": item.get("phone") or None,
        "mobile": item.get("mobile") or None,
        "last_login": item.get("last_login") or None,
        "is_active": _coerce_int(item.get("is_active")),
        "display_name": display_name,
    }


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
    and ``search/User`` returns inconsistent values for joined columns
    (email/realname/etc. are blank on several GLPI 10.x deployments).
    This tool walks the canonical CRUD endpoints — ``Group/{id}/Group_User``
    to obtain ``users_id`` for each direct member, then ``User/{users_id}``
    in parallel for the user record (and ``UserEmail`` sub-resource for
    the default email). Result fields are stable and don't depend on
    ``searchOption`` IDs.

    **Sub-group expansion**

    When ``include_subgroups=true``, this tool first looks up the parent
    group's ``completename`` and OR-searches for users belonging to any
    group whose ``completename`` starts with that prefix. This relies on
    the ``> `` separator GLPI uses for nested group names.

    Permission: ``glpi:read``
    """

    name: ClassVar[str] = "glpi_get_group_members"
    config_namespace: ClassVar[str] = "glpi"
    version: ClassVar[str] = "1.1.0"
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

                # Collect distinct user IDs from Group_User across all target groups.
                user_ids: list[int] = []
                seen: set[int] = set()
                for gid in group_ids:
                    try:
                        rows = await client.get_sub_items(
                            "Group", gid, "Group_User",
                            range=_GROUP_USER_RANGE,
                            expand_dropdowns=False,
                        )
                    except GLPIError as exc:
                        log.warning(
                            "glpi_get_group_members: Group_User lookup failed for group %d: %s",
                            gid, exc,
                        )
                        continue
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        uid = _coerce_int(row.get("users_id"))
                        if uid and uid not in seen:
                            seen.add(uid)
                            user_ids.append(uid)

                total_in_groups = len(user_ids)
                # Trim before fetching details to honour max_results.
                user_ids = user_ids[:max_results]

                # Fetch user details (and emails) in parallel, capped by a semaphore.
                sem = asyncio.Semaphore(_USER_FETCH_CONCURRENCY)

                async def _fetch_one(uid: int) -> Optional[dict]:
                    async with sem:
                        return await _fetch_user_details(client, uid)

                fetched = await asyncio.gather(*(_fetch_one(uid) for uid in user_ids))
                members = [m for m in fetched if m is not None]

                if active_only:
                    members = [m for m in members if (m.get("is_active") in (1, "1", True))]

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

        # Sort by login for stable output.
        members.sort(key=lambda m: (m.get("login") or "").lower())

        return self._success(
            data={
                "summary": {
                    "group_id": group_id,
                    "include_subgroups": include_subgroups,
                    "queried_group_ids": group_ids,
                    "parent_completename": parent_completename,
                    "active_only": active_only,
                    "total_count": total_in_groups,
                    "returned_count": len(members),
                },
                "members": members,
            },
            execution_time_ms=elapsed,
        )


# ── Module-level helpers ───────────────────────────────────────────────────


async def _fetch_user_details(client: GLPIClient, user_id: int) -> Optional[dict]:
    """Fetch a single user (CRUD ``getItem``) plus their default email.

    Returns the flat member dict or ``None`` when the user record cannot
    be retrieved (logged as a warning).
    """
    try:
        item = await client.get_item("User", user_id, expand_dropdowns=False)
    except GLPIError as exc:
        log.warning(
            "glpi_get_group_members: getItem User %d failed: %s", user_id, exc
        )
        return None
    if not isinstance(item, dict) or not item:
        return None

    email: Optional[str] = None
    try:
        emails = await client.get_sub_items(
            "User", user_id, "UserEmail", range="0-9", expand_dropdowns=False,
        )
    except GLPIError as exc:
        log.debug(
            "glpi_get_group_members: UserEmail lookup failed for user %d: %s",
            user_id, exc,
        )
        emails = []

    if emails:
        default = next(
            (e for e in emails if isinstance(e, dict) and _coerce_int(e.get("is_default"))),
            None,
        )
        chosen = default or (emails[0] if isinstance(emails[0], dict) else None)
        if chosen:
            email = chosen.get("email") or None

    return _build_member(item, email, fallback_id=user_id)


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
