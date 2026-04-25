"""gSage AI — GLPI Search tool.

Searches any GLPI itemtype using the GLPI search engine (searchItems endpoint).
Provides convenience parameters for common Ticket queries (status, priority,
keyword, date range), a quick_search parameter for simple multi-field string
matching, and a list_fields action for discovering searchOption field IDs.

Required permission: ``glpi:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult, _tool_session_ctx
from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError
from src.shared.cache.decorator import cached
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# Searchable GLPI itemtypes exposed to the LLM
_ITEMTYPES = [
    "Ticket",
    "Problem",
    "Change",
    "Computer",
    "Monitor",
    "NetworkEquipment",
    "Peripheral",
    "Phone",
    "Printer",
    "Software",
    "SoftwareLicense",
    "User",
    "Group",
    "KnowbaseItem",
    "Contact",
    "Supplier",
    "Contract",
    "Location",
    "Project",
]

# Ticket status codes
_TICKET_STATUSES = {
    "new": 1,
    "assigned": 2,
    "planned": 3,
    "waiting": 4,
    "solved": 5,
    "closed": 6,
}

# Meta status "open" = any status that is not solved/closed.
# Accepted as a value for the ``status`` parameter; expanded into an OR group.
_OPEN_STATUSES = (1, 2, 3, 4)  # new, assigned, planned, waiting

# Ticket/ITIL priority labels
_PRIORITY_LABELS = {1: "very_low", 2: "low", 3: "medium", 4: "high", 5: "very_high", 6: "major"}

_MAX_RESULTS_HARD_LIMIT = 50

# Standard GLPI searchOption field IDs for Ticket
_TICKET_FIELD_NAME = 1            # ticket title / name
_TICKET_FIELD_CONTENT = 7         # full description text
_TICKET_FIELD_STATUS = 12         # status dropdown
_TICKET_FIELD_PRIORITY = 3        # priority
_TICKET_FIELD_DATE_CREATION = 15  # date of creation
_TICKET_FIELD_DATE_MOD = 19       # date last modified
_TICKET_FIELD_DATE_RESOLUTION = 17  # date of resolution
_TICKET_FIELD_REQUESTER = 4       # requester user (glpi_users via _Ticket_User type=1)
_TICKET_FIELD_TECHNICIAN = 5      # assigned technician (glpi_users via _Ticket_User type=2)
_TICKET_FIELD_WATCHER = 22        # observer / watcher (glpi_users via _Ticket_User type=3)
_TICKET_FIELD_TECH_GROUP = 8      # technician group (_Groups_Tickets type=2)
_TICKET_FIELD_REQUESTER_GROUP = 71  # requester group (_Groups_Tickets type=1)

# Known searchOption field IDs for Computer / asset itemtypes
_COMPUTER_FIELD_USER = 70    # Usuário (assigned user)
_COMPUTER_FIELD_GROUP = 71   # Grupo (assigned group)
_COMPUTER_FIELD_SERIAL = 5   # Número de série
_COMPUTER_FIELD_UUID = 47    # UUID
_COMPUTER_FIELD_IP = 126     # IP address
_COMPUTER_FIELD_MAC = 21     # MAC address

# Asset itemtypes that share the same user/group field layout
_ASSET_ITEMTYPES = {
    "Computer", "Monitor", "NetworkEquipment", "Peripheral", "Phone", "Printer"
}

# Fields queried by quick_search per itemtype — list of searchOption IDs searched with OR/contains
_QUICK_SEARCH_FIELDS: dict[str, list[int]] = {
    "Ticket":           [_TICKET_FIELD_NAME, _TICKET_FIELD_CONTENT],
    "Problem":          [1, 7],
    "Change":           [1, 7],
    "KnowbaseItem":     [1, 7],
    "Computer":         [1, _COMPUTER_FIELD_SERIAL, _COMPUTER_FIELD_UUID, _COMPUTER_FIELD_USER],   # name, serial, uuid, user
    "Monitor":          [1, _COMPUTER_FIELD_SERIAL, _COMPUTER_FIELD_USER],
    "NetworkEquipment": [1, _COMPUTER_FIELD_SERIAL, _COMPUTER_FIELD_USER],
    "Peripheral":       [1, _COMPUTER_FIELD_SERIAL, _COMPUTER_FIELD_USER],
    "Phone":            [1, _COMPUTER_FIELD_SERIAL, _COMPUTER_FIELD_USER],
    "Printer":          [1, _COMPUTER_FIELD_SERIAL, _COMPUTER_FIELD_USER],
    "Software":         [1],
    "SoftwareLicense":  [1],
    "User":             [1, 34],   # name, email
    "Group":            [1],
    "Contact":          [1, 5],    # name, email
    "Supplier":         [1],
    "Contract":         [1],
    "Location":         [1],
    "Project":          [1],
}

# Cache TTL for listSearchOptions results (24 hours)
_SEARCH_OPTIONS_CACHE_TTL_SECONDS = 24 * 3600
_TOOL_NAME = "glpi_search"


@cached(
    ttl=_SEARCH_OPTIONS_CACHE_TTL_SECONDS,
    scope="global",
    key_fn=lambda *, itemtype, **_: f"glpi:searchoptions:{itemtype}",
    logical_name=_TOOL_NAME,
)
async def _fetch_list_fields_payload(
    *,
    itemtype: str,
    config: dict,
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> dict:
    """Fetch GLPI listSearchOptions for ``itemtype`` and shape a payload dict.

    Cached globally for 24 h (keyed only by ``itemtype``). Raises ``GLPIError``
    on upstream failure so the caller can surface the error.
    """
    async with GLPIClient(
        url=config.get("url") or None,
        user_token=config.get("user_token") or None,
        app_token=config.get("app_token") or None,
    ) as client:
        raw_options = await client.list_search_options(itemtype)

    fields: list[dict] = []
    for key, value in raw_options.items():
        if not isinstance(value, dict):
            continue
        try:
            field_id = int(key)
        except ValueError:
            continue  # skip section labels like "common"
        fields.append({
            "field_id": field_id,
            "name": value.get("name", ""),
            "field": value.get("field", ""),
            "table": value.get("table", ""),
            "type": value.get("datatype", ""),
            "nosort": value.get("nosort", False),
            "nosearch": value.get("nosearch", False),
            "available_values": value.get("values", {}),
        })

    fields.sort(key=lambda f: f["field_id"])

    return {
        "itemtype": itemtype,
        "field_count": len(fields),
        "note": (
            "Use 'field_id' as the 'field' parameter in criteria. "
            "Fields with nosearch=true cannot be used in criteria. "
            "Check 'available_values' for fields with fixed choices "
            "(use 'equals' searchtype for those)."
        ),
        "fields": fields,
    }


class GlpiSearchTool(BaseTool):
    """Search any GLPI itemtype via the GLPI search engine.

    Use this tool to find GLPI items matching specific criteria.

    **Actions:**

    ``search`` (default)
      Search items of ``itemtype`` using filters. For Ticket searches,
      convenience parameters (``status``, ``priority``, ``keyword``,
      ``date_from`` / ``date_to``) are available. For advanced searches or
      non-Ticket itemtypes, use the raw ``criteria`` array.

      Use ``quick_search`` for a simple free-text search across multiple
      fields at once — no need to know field IDs.

    ``list_fields``
      List all available searchOption field IDs and their names for the
      given ``itemtype``. Results are cached for 24 hours. Use this action
      first when you need to build custom ``criteria`` and don't know the
      field IDs.

    **Common use cases:**

    - ``"show me open critical incidents"``
      → action="search", itemtype="Ticket", status="open", priority=5
    - ``"tickets assigned to raquel.cardoso to handle"``
      → action="search", itemtype="Ticket", technician="raquel.cardoso", status="open"
    - ``"open tickets for user john.doe"`` (as requester)
      → action="search", itemtype="Ticket", requester="john.doe", status="open"
    - ``"tickets watched by security-team group"``
      → action="search", itemtype="Ticket", technician_group="security-team"
    - ``"find tickets related to VPN from last month"``
      → action="search", itemtype="Ticket", keyword="vpn", date_from="2026-03-16"
    - ``"quick search for 'backup failure' across tickets"``
      → action="search", itemtype="Ticket", quick_search="backup failure"
    - ``"search assets for serial ABC123"``
      → action="search", itemtype="Computer", quick_search="ABC123"
    - ``"find computers assigned to user 'heles'"``
      → action="search", itemtype="Computer", user="heles"
    - ``"what machines does john.doe have?"``
      → action="search", itemtype="Computer", user="john.doe"
    - ``"search knowledge base for password reset"``
      → action="search", itemtype="KnowbaseItem", quick_search="password reset"
    - ``"what field IDs can I use to search Tickets?"``
      → action="list_fields", itemtype="Ticket"

    **Search criteria format** (raw, for advanced use):

    Each criterion object requires:

    - ``field``: integer searchOption ID (use ``list_fields`` action to discover IDs)
    - ``searchtype``: ``"contains"``, ``"equals"``, ``"notequals"``,
      ``"lessthan"``, ``"morethan"``
    - ``value``: search value
    - ``link`` (optional for first): ``"AND"`` | ``"OR"`` | ``"AND NOT"`` | ``"OR NOT"``

    Permission: ``glpi:read``
    """

    name: ClassVar[str] = "glpi_search"
    version: ClassVar[str] = "1.2.0"
    summary: ClassVar[str] = "Search any GLPI itemtype (tickets, assets, users) using the GLPI search engine"
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["glpi:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["itemtype"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "list_fields"],
                "default": "search",
                "description": (
                    "Action to perform. "
                    "'search' (default): search items using the provided filters. "
                    "'list_fields': list all available field IDs and names for the itemtype. "
                    "Use list_fields first when you need to build custom criteria and "
                    "don't know which field IDs to use."
                ),
            },
            "itemtype": {
                "type": "string",
                "enum": _ITEMTYPES,
                "description": (
                    "GLPI itemtype to search. Use 'Ticket' for incidents/requests, "
                    "'Computer' / 'NetworkEquipment' / 'Software' for assets, "
                    "'User' / 'Group' for people, 'KnowbaseItem' for the knowledge base."
                ),
            },
            "quick_search": {
                "type": "string",
                "description": (
                    "Simple free-text search: searches the given string across multiple "
                    "relevant fields for the itemtype using OR/contains logic. "
                    "No need to know field IDs. "
                    "Ticket/Problem/Change/KnowbaseItem: searches name and full description. "
                    "Computer/asset types: searches name and serial number. "
                    "User: searches name and email. "
                    "Cannot be combined with 'keyword' or 'criteria'."
                ),
            },
            "keyword": {
                "type": "string",
                "description": (
                    "Full-text search on item name/title only (field 1). "
                    "Convenience shortcut — equivalent to a 'contains' criterion on field 1. "
                    "For broader multi-field search, use 'quick_search' instead."
                ),
            },
            "status": {
                "type": "string",
                "enum": list(_TICKET_STATUSES.keys()) + ["open"],
                "description": (
                    "Ticket status filter (only applies when itemtype='Ticket'). "
                    "Options: new, assigned, planned, waiting, solved, closed. "
                    "Also accepts 'open' (= new OR assigned OR planned OR waiting) "
                    "to match anything that is NOT solved/closed."
                ),
            },
            "priority": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "description": (
                    "Ticket/ITIL priority filter (1=very_low ... 6=major). "
                    "Only applies when itemtype='Ticket', 'Problem', or 'Change'."
                ),
            },
            "date_from": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
                "description": (
                    "Filter items created on or after this date (ISO 8601: YYYY-MM-DD). "
                    "Only applies to itemtypes with a creation date field."
                ),
            },
            "date_to": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
                "description": (
                    "Filter items created on or before this date (ISO 8601: YYYY-MM-DD)."
                ),
            },
            "criteria": {
                "type": "array",
                "description": (
                    "Raw GLPI search criteria array for advanced queries. "
                    "Use the 'list_fields' action first to discover valid field IDs. "
                    "Each object: {field (int), searchtype (string), value (string), "
                    "link (optional: AND/OR/AND NOT/OR NOT)}. "
                    "Cannot be combined with 'quick_search'."
                ),
                "items": {
                    "type": "object",
                    "required": ["field", "searchtype", "value"],
                    "properties": {
                        "field": {"type": "integer"},
                        "searchtype": {
                            "type": "string",
                            "enum": ["contains", "equals", "notequals", "lessthan", "morethan"],
                        },
                        "value": {"type": "string"},
                        "link": {
                            "type": "string",
                            "enum": ["AND", "OR", "AND NOT", "OR NOT"],
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "user": {
                "type": "string",
                "description": (
                    "Filter assets by the assigned user's login name (field 70). "
                    "Applies to Computer, Monitor, NetworkEquipment, Peripheral, Phone, Printer. "
                    "Uses 'contains' matching — partial names work. "
                    "Example: user='heles' finds all computers assigned to any user whose "
                    "login contains 'heles'. "
                    "Cannot be combined with 'quick_search'."
                ),
            },
            "group": {
                "type": "string",
                "description": (
                    "Filter assets by the assigned group name (field 71). "
                    "Applies to Computer, Monitor, NetworkEquipment, Peripheral, Phone, Printer. "
                    "Uses 'contains' matching. "
                    "Cannot be combined with 'quick_search'."
                ),
            },
            "technician": {
                "type": "string",
                "description": (
                    "Ticket filter: assigned technician (field 5 = Ticket_User type=2). "
                    "Only applies when itemtype='Ticket'. "
                    "Accepts a user login (e.g. 'raquel.cardoso') — resolved to user_id "
                    "automatically — or a numeric user_id string. "
                    "Combine with status='open' to see what a technician still needs to handle. "
                    "Cannot be combined with 'quick_search'."
                ),
            },
            "requester": {
                "type": "string",
                "description": (
                    "Ticket filter: requester / reporter (field 4 = Ticket_User type=1). "
                    "Only applies when itemtype='Ticket'. "
                    "Accepts a user login or numeric user_id. "
                    "Cannot be combined with 'quick_search'."
                ),
            },
            "watcher": {
                "type": "string",
                "description": (
                    "Ticket filter: observer / watcher (field 22 = Ticket_User type=3). "
                    "Only applies when itemtype='Ticket'. "
                    "Accepts a user login or numeric user_id. "
                    "Cannot be combined with 'quick_search'."
                ),
            },
            "technician_group": {
                "type": "string",
                "description": (
                    "Ticket filter: assigned technician group (field 8). "
                    "Only applies when itemtype='Ticket'. "
                    "Accepts a group completename or numeric group_id. "
                    "Cannot be combined with 'quick_search'."
                ),
            },
            "requester_group": {
                "type": "string",
                "description": (
                    "Ticket filter: requester group (field 71). "
                    "Only applies when itemtype='Ticket'. "
                    "Accepts a group completename or numeric group_id. "
                    "Cannot be combined with 'quick_search'."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS_HARD_LIMIT,
                "default": 20,
                "description": "Maximum number of results to return (default: 20, max: 50).",
            },
            "sort_field": {
                "type": "integer",
                "description": (
                    "ID of the searchOption to sort results by "
                    "(default: 19 = date_mod descending for Ticket, 1 = name for others). "
                    "Use 'list_fields' to discover valid sort field IDs."
                ),
            },
            "order": {
                "type": "string",
                "enum": ["ASC", "DESC"],
                "default": "DESC",
                "description": "Sort order: ASC or DESC (default: DESC).",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "GLPI REST API base URL (overrides TOOL_GLPI_SEARCH__URL env var).",
            },
            "user_token": {
                "type": "string",
                "description": "GLPI user token (overrides TOOL_GLPI_SEARCH__USER_TOKEN env var).",
            },
            "app_token": {
                "type": "string",
                "description": "GLPI application token (overrides TOOL_GLPI_SEARCH__APP_TOKEN env var).",
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
        action: str = params.get("action") or "search"

        if action == "list_fields":
            return await self._execute_list_fields(agent_context, params, config, t0)
        if action == "search":
            return await self._execute_search(agent_context, params, config, t0)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._failure(
            "INVALID_ACTION",
            f"Unknown action '{action}'. Valid values: 'search', 'list_fields'.",
            retryable=False,
            execution_time_ms=elapsed,
        )

    # ── list_fields ────────────────────────────────────────────────────────

    async def _execute_list_fields(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        t0: float,
    ) -> ToolResult:
        itemtype: str = params["itemtype"]

        session = _tool_session_ctx.get()
        if session is None:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR",
                "DB session not available in execution context.",
                execution_time_ms=elapsed,
            )

        try:
            payload = await _fetch_list_fields_payload(
                itemtype=itemtype,
                config=config,
                session=session,
            )
        except GLPIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.glpi_error or "GLPI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(payload, execution_time_ms=elapsed)

    # ── search ─────────────────────────────────────────────────────────────

    async def _execute_search(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        t0: float,
    ) -> ToolResult:
        itemtype: str = params["itemtype"]
        quick_search: Optional[str] = params.get("quick_search") or None
        keyword: Optional[str] = params.get("keyword") or None
        user: Optional[str] = params.get("user") or None
        group: Optional[str] = params.get("group") or None
        technician: Optional[str] = params.get("technician") or None
        requester: Optional[str] = params.get("requester") or None
        watcher: Optional[str] = params.get("watcher") or None
        technician_group: Optional[str] = params.get("technician_group") or None
        requester_group: Optional[str] = params.get("requester_group") or None
        status: Optional[str] = params.get("status") or None
        priority: Optional[int] = params.get("priority")
        date_from: Optional[str] = params.get("date_from") or None
        date_to: Optional[str] = params.get("date_to") or None
        raw_criteria: list[dict] = params.get("criteria") or []
        max_results: int = min(int(params.get("max_results", 20)), _MAX_RESULTS_HARD_LIMIT)
        sort_field: Optional[int] = params.get("sort_field")
        order: str = params.get("order", "DESC")

        # quick_search is mutually exclusive with criteria, keyword, user, group,
        # and the Ticket-specific technician/requester/watcher/*_group filters.
        if quick_search and raw_criteria:
            return self._failure(
                "PARAM_CONFLICT",
                "'quick_search' cannot be combined with 'criteria'. Use one or the other.",
                retryable=False,
            )
        if quick_search and keyword:
            return self._failure(
                "PARAM_CONFLICT",
                "'quick_search' cannot be combined with 'keyword'. "
                "Use 'quick_search' for broad multi-field search.",
                retryable=False,
            )
        if quick_search and (user or group):
            return self._failure(
                "PARAM_CONFLICT",
                "'quick_search' cannot be combined with 'user' or 'group'. "
                "Use 'quick_search' alone or combine 'user'/'group' with 'criteria'.",
                retryable=False,
            )
        if quick_search and any([technician, requester, watcher, technician_group, requester_group]):
            return self._failure(
                "PARAM_CONFLICT",
                "'quick_search' cannot be combined with Ticket user/group filters "
                "(technician, requester, watcher, technician_group, requester_group).",
                retryable=False,
            )

        # Ticket-specific user/group filters only apply when itemtype == "Ticket".
        if itemtype != "Ticket" and any([technician, requester, watcher, technician_group, requester_group]):
            return self._failure(
                "PARAM_CONFLICT",
                "Ticket filters (technician, requester, watcher, technician_group, "
                f"requester_group) require itemtype='Ticket' (got '{itemtype}').",
                retryable=False,
            )

        # Guard: require at least one filter to avoid dumping the full table
        has_filter = any([
            quick_search, keyword, user, group, status, priority,
            date_from, date_to, raw_criteria,
            technician, requester, watcher, technician_group, requester_group,
        ])
        if not has_filter:
            return self._failure(
                "NO_FILTER",
                (
                    "At least one filter is required (quick_search, keyword, status, priority, "
                    f"date_from/date_to, technician/requester/watcher, or criteria) "
                    f"to avoid returning the full '{itemtype}' table. "
                    "Tip: use quick_search='<term>' for a simple free-text search."
                ),
            )

        # Build criteria
        try:
            async with GLPIClient(
                url=config.get("url") or None,
                user_token=config.get("user_token") or None,
                app_token=config.get("app_token") or None,
            ) as client:
                if quick_search:
                    criteria = _build_quick_search_criteria(itemtype, quick_search)
                else:
                    # Resolve login/name → id for Ticket user/group filters.
                    resolved_technician = await _resolve_user_id(client, technician)
                    resolved_requester = await _resolve_user_id(client, requester)
                    resolved_watcher = await _resolve_user_id(client, watcher)
                    resolved_tech_group = await _resolve_group_id(client, technician_group)
                    resolved_req_group = await _resolve_group_id(client, requester_group)

                    criteria = _build_standard_criteria(
                        itemtype=itemtype,
                        keyword=keyword,
                        user=user,
                        group=group,
                        status=status,
                        priority=priority,
                        date_from=date_from,
                        date_to=date_to,
                        raw_criteria=raw_criteria,
                        technician_id=resolved_technician,
                        requester_id=resolved_requester,
                        watcher_id=resolved_watcher,
                        tech_group_id=resolved_tech_group,
                        requester_group_id=resolved_req_group,
                    )

                # Default sort: date_mod DESC for Ticket, name ASC otherwise
                if sort_field is None:
                    sort_field = _TICKET_FIELD_DATE_MOD if itemtype == "Ticket" else _TICKET_FIELD_NAME

                # Fields to always display
                forcedisplay = [1, 2]
                if itemtype == "Ticket":
                    # id, name, priority, requester, technician, content, status, date_creation, date_mod
                    forcedisplay = [1, 2, 3, 4, 5, 7, 12, 15, 19]

                range_str = f"0-{max_results - 1}"

                log.info(
                    "glpi_search: itemtype=%s criteria_count=%d quick_search=%s",
                    itemtype,
                    len(criteria),
                    bool(quick_search),
                )

                result = await client.search_items(
                    itemtype,
                    criteria,
                    range=range_str,
                    sort=sort_field,
                    order=order,
                    forcedisplay=forcedisplay,
                )
        except GLPIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.glpi_error or "GLPI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("glpi_search: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        total = result.get("totalcount", 0)
        data = result.get("data", [])

        # Build human-readable summary of filters applied
        filters: dict = {}
        if quick_search:
            filters["quick_search"] = quick_search
            filters["fields_searched"] = _QUICK_SEARCH_FIELDS.get(itemtype, [1])
        if keyword:
            filters["keyword"] = keyword
        if user:
            filters["user"] = user
        if group:
            filters["group"] = group
        if technician:
            filters["technician"] = technician
        if requester:
            filters["requester"] = requester
        if watcher:
            filters["watcher"] = watcher
        if technician_group:
            filters["technician_group"] = technician_group
        if requester_group:
            filters["requester_group"] = requester_group
        if status:
            filters["status"] = status
        if priority:
            filters["priority"] = _PRIORITY_LABELS.get(priority, str(priority))
        if date_from:
            filters["date_from"] = date_from
        if date_to:
            filters["date_to"] = date_to
        if raw_criteria:
            filters["criteria_count"] = len(raw_criteria)

        return self._success(
            data={
                "summary": {
                    "itemtype": itemtype,
                    "total_count": total,
                    "returned_count": len(data),
                    "filters_applied": filters,
                },
                "items": data,
            },
            execution_time_ms=elapsed,
        )


# ── Module-level helpers ───────────────────────────────────────────────────


def _build_quick_search_criteria(itemtype: str, term: str) -> list[dict]:
    """Build OR/contains criteria across the common fields for an itemtype."""
    field_ids = _QUICK_SEARCH_FIELDS.get(itemtype, [1])
    criteria = []
    for i, field_id in enumerate(field_ids):
        entry: dict = {"field": field_id, "searchtype": "contains", "value": term}
        if i > 0:
            entry["link"] = "OR"
        criteria.append(entry)
    return criteria


def _build_standard_criteria(
    *,
    itemtype: str,
    keyword: Optional[str],
    user: Optional[str],
    group: Optional[str],
    status: Optional[str],
    priority: Optional[int],
    date_from: Optional[str],
    date_to: Optional[str],
    raw_criteria: list[dict],
    technician_id: Optional[str] = None,
    requester_id: Optional[str] = None,
    watcher_id: Optional[str] = None,
    tech_group_id: Optional[str] = None,
    requester_group_id: Optional[str] = None,
) -> list[dict]:
    """Build criteria list from the convenience filter parameters."""
    criteria: list[dict] = []
    first = True

    def _add(entry: dict, link: str = "AND") -> None:
        """Append a criterion, auto-setting the ``link`` for non-first entries."""
        nonlocal first
        if not first:
            entry.setdefault("link", link)
        criteria.append(entry)
        first = False

    if keyword:
        _add({"field": _TICKET_FIELD_NAME, "searchtype": "contains", "value": keyword})

    if user and itemtype in _ASSET_ITEMTYPES:
        _add({"field": _COMPUTER_FIELD_USER, "searchtype": "contains", "value": user})

    if group and itemtype in _ASSET_ITEMTYPES:
        _add({"field": _COMPUTER_FIELD_GROUP, "searchtype": "contains", "value": group})

    # Ticket user/group filters (resolved IDs).
    if technician_id and itemtype == "Ticket":
        _add({"field": _TICKET_FIELD_TECHNICIAN, "searchtype": "equals", "value": technician_id})
    if requester_id and itemtype == "Ticket":
        _add({"field": _TICKET_FIELD_REQUESTER, "searchtype": "equals", "value": requester_id})
    if watcher_id and itemtype == "Ticket":
        _add({"field": _TICKET_FIELD_WATCHER, "searchtype": "equals", "value": watcher_id})
    if tech_group_id and itemtype == "Ticket":
        _add({"field": _TICKET_FIELD_TECH_GROUP, "searchtype": "equals", "value": tech_group_id})
    if requester_group_id and itemtype == "Ticket":
        _add({"field": _TICKET_FIELD_REQUESTER_GROUP, "searchtype": "equals", "value": requester_group_id})

    if status and itemtype == "Ticket":
        status_lower = status.lower()
        if status_lower == "open":
            # Meta-status: open = new OR assigned OR planned OR waiting.
            # Wrap in an OR group so it composes with AND against the other filters.
            for i, sid in enumerate(_OPEN_STATUSES):
                entry: dict = {
                    "field": _TICKET_FIELD_STATUS,
                    "searchtype": "equals",
                    "value": str(sid),
                }
                if i == 0:
                    # First open-group entry keeps AND against prior filters.
                    _add(entry, link="AND")
                else:
                    # Subsequent are OR to chain within the open group.
                    entry["link"] = "OR"
                    criteria.append(entry)
        else:
            status_id = _TICKET_STATUSES.get(status_lower)
            if status_id is not None:
                _add({
                    "field": _TICKET_FIELD_STATUS,
                    "searchtype": "equals",
                    "value": str(status_id),
                })

    if priority and itemtype in ("Ticket", "Problem", "Change"):
        _add({
            "field": _TICKET_FIELD_PRIORITY,
            "searchtype": "equals",
            "value": str(priority),
        })

    if date_from and itemtype == "Ticket":
        _add({
            "field": _TICKET_FIELD_DATE_CREATION,
            "searchtype": "morethan",
            "value": date_from,
        })

    if date_to and itemtype == "Ticket":
        _add({
            "field": _TICKET_FIELD_DATE_CREATION,
            "searchtype": "lessthan",
            "value": date_to,
        })

    # Merge raw criteria — add AND link to first raw item if standard criteria already populated
    for i, crit in enumerate(raw_criteria):
        merged = dict(crit)
        if criteria and i == 0 and "link" not in merged:
            merged["link"] = "AND"
        criteria.append(merged)

    return criteria


async def _resolve_user_id(client: GLPIClient, value: Optional[str]) -> Optional[str]:
    """Resolve a user login (``'raquel.cardoso'``) to the numeric GLPI user_id.

    - Returns ``None`` when ``value`` is falsy.
    - Returns ``value`` unchanged when it is already a digit string.
    - Otherwise searches ``User`` by name/realname/login and returns the
      first match's id as string. Raises :class:`GLPIError` on API failure;
      returns the original string if no user is found (the caller will get
      zero results from the ticket search, which is a clear signal).
    """
    if not value:
        return None
    if value.isdigit():
        return value

    # Search User by field 1 (name) with 'contains' — first hit wins.
    try:
        result = await client.search_items(
            "User",
            [{"field": 1, "searchtype": "contains", "value": value}],
            range="0-0",
            forcedisplay=[2],  # id
        )
    except GLPIError:
        raise

    data = result.get("data") or []
    if not data:
        log.warning("glpi_search: could not resolve user login '%s' to an id", value)
        return value  # let the search return empty rather than failing opaquely

    first = data[0]
    # field 2 is id; GLPI may return string keys.
    uid = first.get("2") or first.get(2)
    if uid is None:
        return value
    return str(uid)


async def _resolve_group_id(client: GLPIClient, value: Optional[str]) -> Optional[str]:
    """Resolve a group completename to the numeric GLPI group_id.

    Same semantics as :func:`_resolve_user_id` but against the ``Group`` itemtype.
    """
    if not value:
        return None
    if value.isdigit():
        return value

    try:
        result = await client.search_items(
            "Group",
            [{"field": 1, "searchtype": "contains", "value": value}],
            range="0-0",
            forcedisplay=[2],
        )
    except GLPIError:
        raise

    data = result.get("data") or []
    if not data:
        log.warning("glpi_search: could not resolve group '%s' to an id", value)
        return value

    first = data[0]
    gid = first.get("2") or first.get(2)
    if gid is None:
        return value
    return str(gid)
