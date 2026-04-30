"""gSage AI — GLPI Update Ticket tool.

Performs actions on an existing GLPI Ticket:
  - update       — change ticket fields (status, priority, category, etc.)
  - add_followup — add a follow-up comment
  - add_solution — record the resolution / solution
  - assign       — assign to a user or group
  - close        — close the ticket (optionally with a solution)
  - escalate_priority — raise ticket priority by one level

All actions require human-in-the-loop approval before execution.

Required permission: ``glpi:write``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_PRIORITY_MAX = 6

# GLPI ticket status codes
_STATUS_SOLVED = 5
_STATUS_CLOSED = 6


class GlpiUpdateTicketTool(BaseTool):
    """Perform an action on an existing GLPI Ticket.

    **Available actions:**

    | Action             | Description                                              |
    |--------------------|----------------------------------------------------------|
    | ``update``         | Modify ticket fields (status, priority, category, etc.)  |
    | ``add_followup``   | Append a follow-up comment (public or private)           |
    | ``add_solution``   | Record the resolution and mark ticket as Solved          |
    | ``assign``         | Assign ticket to a technician user and/or group          |
    | ``close``          | Close the ticket (status → CLOSED)                       |
    | ``escalate_priority`` | Raise priority by one level (capped at 6 = Major)    |

    **Action-specific parameters:**

    *add_followup*: ``followup_content`` (required), ``is_private`` (optional, default false)

    *add_solution*: ``solution_content`` (required), ``solution_type_id`` (optional GLPI SolutionType ID)

    *assign*: at least one of ``assigned_user_id`` or ``assigned_group_id``

    *close*: no extra params (optionally provide ``solution_content`` to add a solution first)

    *update*: any combination of ``status``, ``priority``, ``urgency``, ``impact``,
    ``category_id``, ``name``, ``content``

    **Status codes:**
    1=New, 2=Assigned, 3=Planned, 4=Waiting, 5=Solved, 6=Closed

    Requires **human approval** before execution.

    Permission: ``glpi:write``
    """

    name: ClassVar[str] = "glpi_update_ticket"
    config_namespace: ClassVar[str] = "glpi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Update an existing GLPI ticket: add notes, change status, or assign users"
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["glpi:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["ticket_id", "action"],
        "properties": {
            "ticket_id": {
                "type": "integer",
                "minimum": 1,
                "description": "ID of the GLPI Ticket to act upon.",
            },
            "action": {
                "type": "string",
                "enum": [
                    "update",
                    "add_followup",
                    "add_solution",
                    "assign",
                    "close",
                    "escalate_priority",
                ],
                "description": "Action to perform on the ticket.",
            },
            # ── update ──────────────────────────────────────────────────
            "status": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "description": (
                    "New ticket status: 1=New, 2=Assigned, 3=Planned, "
                    "4=Waiting, 5=Solved, 6=Closed. Used by action='update'."
                ),
            },
            "priority": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "description": "New priority (1–6). Used by action='update' or 'escalate_priority'.",
            },
            "urgency": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "New urgency (1–5). Used by action='update'.",
            },
            "impact": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "New impact (1–5). Used by action='update'.",
            },
            "category_id": {
                "type": "integer",
                "minimum": 1,
                "description": "GLPI ITILCategory ID. Used by action='update'.",
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "New ticket title. Used by action='update'.",
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "New ticket description. Used by action='update'.",
            },
            # ── add_followup ─────────────────────────────────────────────
            "followup_content": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Text of the follow-up comment to add. "
                    "Required for action='add_followup'."
                ),
            },
            "is_private": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Whether the follow-up is visible only to technicians. "
                    "Used by action='add_followup'. Default: false (public)."
                ),
            },
            # ── add_solution ─────────────────────────────────────────────
            "solution_content": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Description of the resolution. "
                    "Required for action='add_solution'. "
                    "Also used by action='close' to record a solution before closing."
                ),
            },
            "solution_type_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "GLPI SolutionType ID. Optional for action='add_solution'. "
                    "Common IDs vary by GLPI install — query /SolutionType to discover."
                ),
            },
            # ── assign ───────────────────────────────────────────────────
            "assigned_user_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "GLPI User ID of the technician to assign. "
                    "Used by action='assign'. At least one of assigned_user_id / "
                    "assigned_group_id is required."
                ),
            },
            "assigned_group_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "GLPI Group ID to assign the ticket to. "
                    "Used by action='assign'."
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

        ticket_id: int = int(params["ticket_id"])
        action: str = params["action"]

        log.info(
            "glpi_update_ticket: ticket=%d, action=%s, config_keys=%s",
            ticket_id, action, [k for k, v in config.items() if v],
        )

        try:
            async with GLPIClient(
                url=config.get("url") or None,
                user_token=config.get("user_token") or None,
                app_token=config.get("app_token") or None,
            ) as client:
                result = await self._dispatch(client, ticket_id, action, params)
        except GLPIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.glpi_error or "GLPI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )
        except ValueError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("glpi_update_ticket: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"ticket_id": ticket_id, "action": action, "result": result},
            execution_time_ms=elapsed,
        )

    # ── Action dispatchers ───────────────────────────────────────────────────

    async def _dispatch(
        self,
        client: GLPIClient,
        ticket_id: int,
        action: str,
        params: dict,
    ) -> dict:
        if action == "update":
            return await self._action_update(client, ticket_id, params)
        if action == "add_followup":
            return await self._action_add_followup(client, ticket_id, params)
        if action == "add_solution":
            return await self._action_add_solution(client, ticket_id, params)
        if action == "assign":
            return await self._action_assign(client, ticket_id, params)
        if action == "close":
            return await self._action_close(client, ticket_id, params)
        if action == "escalate_priority":
            return await self._action_escalate_priority(client, ticket_id, params)
        raise ValueError(f"Unknown action: {action!r}")

    async def _action_update(self, client: GLPIClient, ticket_id: int, params: dict) -> dict:
        fields: dict = {"id": ticket_id}
        if "status" in params:
            fields["status"] = int(params["status"])
        if "priority" in params:
            fields["priority"] = int(params["priority"])
        if "urgency" in params:
            fields["urgency"] = int(params["urgency"])
        if "impact" in params:
            fields["impact"] = int(params["impact"])
        if "category_id" in params:
            fields["itilcategories_id"] = int(params["category_id"])
        if "name" in params:
            fields["name"] = params["name"]
        if "content" in params:
            fields["content"] = params["content"]

        if len(fields) == 1:  # only 'id' — nothing to change
            raise ValueError("No update fields provided for action='update'.")

        result = await client.update_item("Ticket", ticket_id, fields)
        return {"updated": True, "response": result}

    async def _action_add_followup(
        self, client: GLPIClient, ticket_id: int, params: dict
    ) -> dict:
        content = params.get("followup_content")
        if not content:
            raise ValueError("followup_content is required for action='add_followup'.")
        is_private: bool = bool(params.get("is_private", False))
        result = await client.add_item(
            "TicketFollowup",
            {
                "tickets_id": ticket_id,
                "content": content,
                "is_private": 1 if is_private else 0,
            },
        )
        return {"followup_id": result.get("id"), "message": result.get("message", "")}

    async def _action_add_solution(
        self, client: GLPIClient, ticket_id: int, params: dict
    ) -> dict:
        content = params.get("solution_content")
        if not content:
            raise ValueError("solution_content is required for action='add_solution'.")
        input_data: dict = {
            "itemtype": "Ticket",
            "items_id": ticket_id,
            "content": content,
        }
        if "solution_type_id" in params:
            input_data["solutiontypes_id"] = int(params["solution_type_id"])
        result = await client.add_item("ITILSolution", input_data)
        return {"solution_id": result.get("id"), "message": result.get("message", "")}

    async def _action_assign(
        self, client: GLPIClient, ticket_id: int, params: dict
    ) -> dict:
        user_id = params.get("assigned_user_id")
        group_id = params.get("assigned_group_id")
        if not user_id and not group_id:
            raise ValueError(
                "At least one of assigned_user_id or assigned_group_id is required "
                "for action='assign'."
            )
        assignees: list[dict] = []
        if user_id:
            assignees.append({"items_id": int(user_id), "itemtype": "User"})
        if group_id:
            assignees.append({"items_id": int(group_id), "itemtype": "Group"})
        result = await client.update_item(
            "Ticket",
            ticket_id,
            {"id": ticket_id, "_actors": {"assign": assignees}},
        )
        return {"assigned": True, "response": result}

    async def _action_close(
        self, client: GLPIClient, ticket_id: int, params: dict
    ) -> dict:
        ops: list[str] = []
        # Optionally add a solution before closing
        solution_content = params.get("solution_content")
        if solution_content:
            sol_input: dict = {
                "itemtype": "Ticket",
                "items_id": ticket_id,
                "content": solution_content,
            }
            if "solution_type_id" in params:
                sol_input["solutiontypes_id"] = int(params["solution_type_id"])
            sol_result = await client.add_item("ITILSolution", sol_input)
            ops.append(f"solution_id={sol_result.get('id')}")

        # Close the ticket
        result = await client.update_item(
            "Ticket",
            ticket_id,
            {"id": ticket_id, "status": _STATUS_CLOSED},
        )
        ops.append("status=CLOSED")
        return {"closed": True, "operations": ops, "response": result}

    async def _action_escalate_priority(
        self, client: GLPIClient, ticket_id: int, params: dict
    ) -> dict:
        # Fetch current ticket to read priority
        ticket = await client.get_item("Ticket", ticket_id, expand_dropdowns=False)
        current_priority: int = int(ticket.get("priority", 3))
        new_priority = min(current_priority + 1, _PRIORITY_MAX)
        if new_priority == current_priority:
            return {
                "escalated": False,
                "reason": f"Ticket already at maximum priority ({_PRIORITY_MAX}).",
                "current_priority": current_priority,
            }
        result = await client.update_item(
            "Ticket",
            ticket_id,
            {"id": ticket_id, "priority": new_priority},
        )
        return {
            "escalated": True,
            "previous_priority": current_priority,
            "new_priority": new_priority,
            "response": result,
        }
