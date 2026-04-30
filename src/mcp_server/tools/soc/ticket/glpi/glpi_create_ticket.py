"""gSage AI — GLPI Create Ticket tool.

Creates a new GLPI Ticket (Incident or Service Request).
Requires human-in-the-loop approval before execution.

Required permission: ``glpi:write``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional
from urllib.parse import urljoin

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class GlpiCreateTicketTool(BaseTool):
    """Create a new GLPI Ticket (Incident or Service Request).

    **Ticket types:**

    - ``1`` — Incident (unexpected failure, outage, degradation)
    - ``2`` — Request (change, new service, additional resource)

    **Priority / UrgencyImpact matrix (GLPI standard):**

    | Value | Label    |
    |-------|----------|
    | 1     | Very low |
    | 2     | Low      |
    | 3     | Medium   |
    | 4     | High     |
    | 5     | Very high |
    | 6     | Major    |

    GLPI automatically computes the final priority from urgency × impact if
    both are provided; otherwise the ``priority`` field is used directly.

    **Examples:**

    - ``"abre chamado de incidente: servidor web fora do ar"``
      → name="...", content="...", type=1, priority=4
    - ``"solicita acesso para o usuário João ao sistema X"``
      → name="Solicitação de acesso — João", type=2, priority=2

    Requires **human approval** before submission.

    Permission: ``glpi:write``
    """

    name: ClassVar[str] = "glpi_create_ticket"
    config_namespace: ClassVar[str] = "glpi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Create a new GLPI Ticket (Incident or Service Request) with title, description, and priority"
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["glpi:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["name", "content"],
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "Ticket title / subject.",
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "Full description of the issue or request (HTML or plain text).",
            },
            "type": {
                "type": "string",
                "enum": ["1", "2"],
                "default": "1",
                "description": "Ticket type: 1 = Incident, 2 = Request.",
            },
            "priority": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "default": 3,
                "description": (
                    "Ticket priority (1=Very low … 6=Major). "
                    "Used when urgency and impact are not provided. Default: 3 (Medium)."
                ),
            },
            "urgency": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": (
                    "Urgency (1–5). When combined with impact, GLPI computes the priority automatically."
                ),
            },
            "impact": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Impact (1–5). When combined with urgency, GLPI computes priority.",
            },
            "category_id": {
                "type": "integer",
                "minimum": 1,
                "description": "GLPI ITILCategory ID to classify this ticket.",
            },
            "requester_user_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "GLPI User ID of the person reporting / requesting. "
                    "Defaults to the user associated with the user_token."
                ),
            },
            "assigned_user_id": {
                "type": "integer",
                "minimum": 1,
                "description": "GLPI User ID to assign as technician (tech or expert).",
            },
            "assigned_group_id": {
                "type": "integer",
                "minimum": 1,
                "description": "GLPI Group ID to assign the ticket to.",
            },
            "entity_id": {
                "type": "integer",
                "minimum": 0,
                "description": "GLPI Entity ID (department / sub-entity). Defaults to root entity.",
            },
            "location_id": {
                "type": "integer",
                "minimum": 1,
                "description": "GLPI Location ID for the affected site or room.",
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

        # Build ticket input dict — only include provided fields
        input_data: dict = {
            "name": params["name"],
            "content": params["content"],
        }

        if "type" in params:
            input_data["type"] = int(params["type"])
        if "priority" in params:
            input_data["priority"] = int(params["priority"])
        if "urgency" in params:
            input_data["urgency"] = int(params["urgency"])
        if "impact" in params:
            input_data["impact"] = int(params["impact"])
        if "category_id" in params:
            input_data["itilcategories_id"] = int(params["category_id"])
        if "entity_id" in params:
            input_data["entities_id"] = int(params["entity_id"])
        if "location_id" in params:
            input_data["locations_id"] = int(params["location_id"])

        # GLPI actors API: requester, assigned user, assigned group
        actors: list[dict] = []
        if "requester_user_id" in params:
            actors.append({
                "type": 1,  # Requester
                "items_id": int(params["requester_user_id"]),
                "itemtype": "User",
            })
        if "assigned_user_id" in params:
            actors.append({
                "type": 2,  # Assigned
                "items_id": int(params["assigned_user_id"]),
                "itemtype": "User",
            })
        if "assigned_group_id" in params:
            actors.append({
                "type": 2,  # Assigned
                "items_id": int(params["assigned_group_id"]),
                "itemtype": "Group",
            })
        if actors:
            input_data["_actors"] = {
                "requester": [a for a in actors if a["type"] == 1],
                "assign": [a for a in actors if a["type"] == 2],
            }

        log.info(
            "glpi_create_ticket: config_keys=%s",
            [k for k, v in config.items() if v],
        )

        try:
            async with GLPIClient(
                url=config.get("url") or None,
                user_token=config.get("user_token") or None,
                app_token=config.get("app_token") or None,
            ) as client:
                result = await client.add_item("Ticket", input_data)
        except GLPIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.glpi_error or "GLPI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("glpi_create_ticket: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)

        ticket_id = result.get("id")
        message = result.get("message", "")

        # Build a direct URL to the ticket if the GLPI base URL is available
        glpi_url = config.get("url") or ""
        ticket_url: Optional[str] = None
        if glpi_url and ticket_id:
            base = glpi_url.rstrip("/").replace("/apirest.php", "").replace("/api", "")
            ticket_url = f"{base}/front/ticket.form.php?id={ticket_id}"

        return self._success(
            data={
                "ticket_id": ticket_id,
                "message": message,
                "ticket_url": ticket_url,
                "input": {
                    "name": params["name"],
                    "type": input_data.get("type", 1),
                    "priority": input_data.get("priority", 3),
                },
            },
            execution_time_ms=elapsed,
        )
