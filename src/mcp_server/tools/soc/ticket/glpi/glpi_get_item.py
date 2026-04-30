"""gSage AI — GLPI Get Item tool.

Retrieves a single GLPI item by type and ID, with optional sub-items
(followups, solutions, tasks, linked assets, logs).

Required permission: ``glpi:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

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
    "Entity",
]

# Sub-item types supported per parent type
_TICKET_SUBITEM_TYPES = ["TicketFollowup", "ITILSolution", "TicketTask", "Item_Ticket", "Log"]
_COMPUTER_SUBITEM_TYPES = ["Item_Line", "Log", "NetworkPort"]
_GENERIC_SUBITEM_TYPES = ["Log", "Document_Item"]

# Compact-mode field whitelists per itemtype.
# Returned as a small, LLM-friendly subset of the full GLPI item record
# (which can have 100+ fields including UI/session noise like cookie_token_date,
# pallete, date_format, etc.). Only fields useful for analysis are kept.
_COMPACT_FIELDS: dict[str, list[str]] = {
    "Ticket": [
        "id", "name", "status", "priority", "urgency", "impact", "type",
        "itilcategories_id", "requesttypes_id",
        "date", "date_creation", "date_mod", "solvedate", "closedate",
        "time_to_resolve", "time_to_own",
        "content",
        "users_id_recipient", "users_id_lastupdater",
        "_users_id_assign", "_groups_id_assign",
        "_users_id_requester", "_groups_id_requester",
        "entities_id", "locations_id",
    ],
    "User": [
        "id", "name", "realname", "firstname", "phone", "phone2", "mobile",
        "is_active", "last_login", "profiles_id", "usertitles_id",
        "usercategories_id", "entities_id", "locations_id",
        "groups_id", "_useremails",
    ],
    "Group": [
        "id", "name", "completename", "comment", "is_recursive",
        "is_requester", "is_assign", "groups_id", "entities_id",
    ],
    "Computer": [
        "id", "name", "serial", "otherserial", "uuid",
        "computertypes_id", "computermodels_id", "manufacturers_id",
        "states_id", "users_id", "groups_id",
        "locations_id", "entities_id",
        "date_creation", "date_mod", "is_deleted",
    ],
}
# Fallback: when no whitelist is defined for an itemtype, drop these noisy keys.
_COMPACT_DROP_KEYS: set[str] = {
    "cookie_token", "cookie_token_date", "personal_token", "personal_token_date",
    "api_token", "api_token_date", "password", "password_last_update",
    "date_format", "number_format", "names_format", "csv_delimiter",
    "pallete", "palette", "theme", "highcontrast_css", "language",
    "begin_date", "end_date", "display_count_on_home", "is_ids_visible",
    "keep_devices_when_purging_an_item", "notification_to_myself",
    "backcreated", "task_state", "refresh_views", "set_default_tech",
    "set_default_requester", "priority_1", "priority_2", "priority_3",
    "priority_4", "priority_5", "priority_6", "followup_private",
    "task_private", "default_requesttypes_id", "layout",
    "lock_autolock_mode", "lock_directunlock_notification",
    "timezone",
}


def _compact_item(itemtype: str, item: dict) -> dict:
    """Return a slimmed-down view of a GLPI item for LLM consumption.

    - If ``itemtype`` has a whitelist in ``_COMPACT_FIELDS``, keep only those keys
      that are present in the source dict.
    - Otherwise, return all keys except those in ``_COMPACT_DROP_KEYS``.
    """
    if not isinstance(item, dict):
        return item
    whitelist = _COMPACT_FIELDS.get(itemtype)
    if whitelist:
        return {k: item[k] for k in whitelist if k in item}
    return {k: v for k, v in item.items() if k not in _COMPACT_DROP_KEYS}


class GlpiGetItemTool(BaseTool):
    """Retrieve a single GLPI item by type and ID.

    Returns the full item record.  For Tickets, additional sub-item types can
    be fetched in the same call:

    - **TicketFollowup** — comments / follow-ups on the ticket
    - **ITILSolution** — resolution / solution records
    - **TicketTask** — tasks linked to the ticket
    - **Item_Ticket** — hardware / assets linked to the ticket
    - **Log** — historical change log

    For asset types (Computer, NetworkEquipment, etc.):

    - ``with_softwares=true`` — list of installed software
    - ``with_connections=true`` — connected peripherals
    - ``with_networkports=true`` — network port / IP information
    - ``with_devices=true`` — internal hardware components
    - ``with_infocoms=true`` — financial/warranty information

    **Examples:**

    - ``"mostra os detalhes do chamado 4212"``
      → itemtype="Ticket", id=4212, sub_items=["TicketFollowup","ITILSolution"]
    - ``"informações de rede do servidor APP-SRV-01 (id 87)"``
      → itemtype="Computer", id=87, with_networkports=true
    - ``"chamados abertos para o usuário 15"``
      → itemtype="User", id=15 (use glpi_search for their tickets)

    Permission: ``glpi:read``
    """

    name: ClassVar[str] = "glpi_get_item"
    config_namespace: ClassVar[str] = "glpi"
    version: ClassVar[str] = "1.1.0"
    summary: ClassVar[str] = "Retrieve a single GLPI item (ticket, asset, user) by its numeric ID"
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["glpi:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["itemtype", "id"],
        "properties": {
            "itemtype": {
                "type": "string",
                "enum": _ITEMTYPES,
                "description": "GLPI itemtype of the item to retrieve.",
            },
            "id": {
                "type": "integer",
                "minimum": 1,
                "description": "Numeric ID of the GLPI item.",
            },
            "expand_dropdowns": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Replace numeric dropdown IDs with their text labels "
                    "(e.g. status 1 → 'New'). Default: true."
                ),
            },
            # Ticket-specific sub-items
            "sub_items": {
                "type": "array",
                "description": (
                    "List of sub-item types to fetch alongside the main item. "
                    "For Tickets: TicketFollowup, ITILSolution, TicketTask, "
                    "Item_Ticket, Log."
                ),
                "items": {
                    "type": "string",
                    "enum": _TICKET_SUBITEM_TYPES + _GENERIC_SUBITEM_TYPES,
                },
                "uniqueItems": True,
            },
            "sub_items_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
                "description": "Maximum sub-items to return per sub-item type (default: 20).",
            },
            # Asset-specific inline expansions (pass to getItem)
            "with_tickets": {
                "type": "boolean",
                "default": False,
                "description": "Include linked tickets (asset types). Default: false.",
            },
            "with_devices": {
                "type": "boolean",
                "default": False,
                "description": "Include internal hardware components (Computer). Default: false.",
            },
            "with_softwares": {
                "type": "boolean",
                "default": False,
                "description": "Include installed software list (Computer). Default: false.",
            },
            "with_connections": {
                "type": "boolean",
                "default": False,
                "description": "Include connected peripherals (Computer). Default: false.",
            },
            "with_networkports": {
                "type": "boolean",
                "default": False,
                "description": "Include network ports / IPs (Computer, NetworkEquipment). Default: false.",
            },
            "with_infocoms": {
                "type": "boolean",
                "default": False,
                "description": "Include financial / warranty info. Default: false.",
            },
            "with_contracts": {
                "type": "boolean",
                "default": False,
                "description": "Include linked contracts. Default: false.",
            },
            "with_documents": {
                "type": "boolean",
                "default": False,
                "description": "Include linked documents. Default: false.",
            },
            "compact": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Return only the most relevant fields (id, name, status, priority, "
                    "category, technician, dates, content, etc.) instead of the full "
                    "GLPI record. Drops UI/session noise (cookie_token_date, palette, "
                    "date_format, etc.). Greatly reduces response size and LLM context cost."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "GLPI REST API base URL."},
            "user_token": {"type": "string", "description": "GLPI user token."},
            "app_token": {"type": "string", "description": "GLPI application token."},
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

        itemtype: str = params["itemtype"]
        item_id: int = int(params["id"])
        expand_dropdowns: bool = params.get("expand_dropdowns", True)
        sub_items_req: list[str] = params.get("sub_items") or []
        sub_items_limit: int = min(int(params.get("sub_items_limit", 20)), 100)
        sub_range = f"0-{sub_items_limit - 1}"

        with_tickets: bool = params.get("with_tickets", False)
        with_devices: bool = params.get("with_devices", False)
        with_softwares: bool = params.get("with_softwares", False)
        with_connections: bool = params.get("with_connections", False)
        with_networkports: bool = params.get("with_networkports", False)
        with_infocoms: bool = params.get("with_infocoms", False)
        with_contracts: bool = params.get("with_contracts", False)
        with_documents: bool = params.get("with_documents", False)
        compact: bool = params.get("compact", False)

        log.info(
            "glpi_get_item: itemtype=%s, id=%d, config_keys=%s",
            itemtype, item_id, [k for k, v in config.items() if v],
        )

        try:
            async with GLPIClient(
                url=config.get("url") or None,
                user_token=config.get("user_token") or None,
                app_token=config.get("app_token") or None,
            ) as client:
                # Fetch main item
                item = await client.get_item(
                    itemtype,
                    item_id,
                    expand_dropdowns=expand_dropdowns,
                    with_tickets=with_tickets,
                    with_devices=with_devices,
                    with_softwares=with_softwares,
                    with_connections=with_connections,
                    with_networkports=with_networkports,
                    with_infocoms=with_infocoms,
                    with_contracts=with_contracts,
                    with_documents=with_documents,
                )

                # Fetch requested sub-items
                sub_item_results: dict[str, list[dict]] = {}
                for sub_type in sub_items_req:
                    try:
                        sub_list = await client.get_sub_items(
                            itemtype,
                            item_id,
                            sub_type,
                            range=sub_range,
                            expand_dropdowns=expand_dropdowns,
                        )
                        sub_item_results[sub_type] = sub_list
                    except GLPIError as exc:
                        log.warning(
                            "glpi_get_item: sub-item fetch failed type=%s id=%d sub=%s err=%s",
                            itemtype, item_id, sub_type, exc,
                        )
                        sub_item_results[sub_type] = []

        except GLPIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.glpi_error or "GLPI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("glpi_get_item: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)

        if compact:
            item = _compact_item(itemtype, item)

        result: dict = {"itemtype": itemtype, "id": item_id, "item": item}
        if sub_item_results:
            result["sub_items"] = sub_item_results

        return self._success(data=result, execution_time_ms=elapsed)
