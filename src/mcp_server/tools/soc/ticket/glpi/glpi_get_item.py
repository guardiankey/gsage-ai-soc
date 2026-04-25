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
    version: ClassVar[str] = "1.0.0"
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

        result: dict = {"itemtype": itemtype, "id": item_id, "item": item}
        if sub_item_results:
            result["sub_items"] = sub_item_results

        return self._success(data=result, execution_time_ms=elapsed)
