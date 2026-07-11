"""collect_user_input — Agent-invokable tool for collecting user input.

This tool bridges the gap between the AI agent and the Interaction Service.
The agent passes field definitions as JSON parameters; the tool delegates
to :class:`InteractionService` with ``REPLAN_AGENT`` mode so that the
user's responses flow back to the agent for replanning.

Usage (by the agent, via MCP)::

    run_discovered_tool(
        tool_name="collect_user_input",
        params={
            "title": "Cadastro de Cliente",
            "fields": [
                {"id": "nome", "field_type": "text", "label": "Nome", "required": true},
                {"id": "idade", "field_type": "number", "label": "Idade", "min": 18},
            ],
        },
    )

Conditional fields (depends_on)::

    Fields can declare ``depends_on`` to control visibility based on
    another field's value.  Example::

        {
            "id": "subtipo_tic",
            "field_type": "select",
            "label": "Qual o tipo de solução de TIC?",
            "options": [...],
            "depends_on": {"field": "tic_envolve", "value": true}
        }

    The field is only shown when the referenced field has the specified value.
    Initially only ``equals`` semantics are supported (the value must match
    exactly).

The agent never sees the form — it receives the user's responses as a
``[INTERACTION_RESPONSE]`` context block and can replan accordingly.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.interaction.enums import ResumeMode
from src.shared.interaction.interactions import FormInteraction
from src.shared.security.context import AgentContext


class CollectUserInputTool(BaseTool):
    """Collect structured information from the user via a dynamic form.

    The agent specifies the fields as a JSON array; the tool builds a
    :class:`FormInteraction` and delegates to the Interaction Service
    in ``REPLAN_AGENT`` mode.  The user's responses are returned to
    the agent as an ``[INTERACTION_RESPONSE]`` block.

    Permission: ``interaction:collect``
    """

    name: ClassVar[str] = "collect_user_input"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Show a dynamic form to the user and return their responses "
        "to the agent for replanning"
    )
    category: ClassVar[str] = "utility"
    core_tool: ClassVar[bool] = False  # discoverable via search_tools
    available: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["interaction:collect"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 900  # 15 min — user may take time to fill
    use_circuit_breaker: ClassVar[bool] = False

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    requires_config: ClassVar[bool] = False

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Form title shown in the modal header",
            },
            "description": {
                "type": "string",
                "description": "Optional subtitle / description below the title",
            },
            "fields": {
                "type": "array",
                "description": "List of field definitions the user must fill",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "Field identifier (returned as key in the response). "
                                "Use short, descriptive names: 'nome', 'email', 'setor'."
                            ),
                        },
                        "field_type": {
                            "type": "string",
                            "enum": [
                                "text", "textarea", "number",
                                "select", "checkbox", "checkbox_group", "radio", "date",
                            ],
                            "description": "Type of input to render",
                        },
                        "label": {
                            "type": "string",
                            "description": "Human-readable label shown above the field",
                        },
                        "required": {
                            "type": "boolean",
                            "description": "Whether the user must fill this field",
                        },
                        "placeholder": {
                            "type": "string",
                            "description": "Placeholder text inside the input",
                        },
                        "hint": {
                            "type": "string",
                            "description": "Helper text shown below the field",
                        },
                        "description": {
                            "type": "string",
                            "description": "Longer description for the field",
                        },
                        "value": {
                            "description": (
                                "Pre-filled value. Use when you have a sensible "
                                "default to suggest to the user."
                            ),
                        },
                        "options": {
                            "type": "array",
                            "description": "Choices for select/radio fields",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "value": {"type": "string"},
                                    "label": {"type": "string"},
                                },
                                "required": ["value", "label"],
                            },
                        },
                        "min": {
                            "type": "number",
                            "description": "Minimum value (number fields)",
                        },
                        "max": {
                            "type": "number",
                            "description": "Maximum value (number fields)",
                        },
                        "max_length": {
                            "type": "integer",
                            "description": "Max character length (text/textarea)",
                        },
                        "min_length": {
                            "type": "integer",
                            "description": "Min character length (text fields)",
                        },
                        "rows": {
                            "type": "integer",
                            "description": "Visible rows (textarea fields)",
                        },
                        "depends_on": {
                            "type": "object",
                            "description": (
                                "Conditional visibility: field is only shown when "
                                "the referenced field has the specified value. "
                                "Initially only supports 'equals' semantics."
                            ),
                            "properties": {
                                "field": {
                                    "type": "string",
                                    "description": "The 'id' of the field this depends on.",
                                },
                                "value": {
                                    "description": (
                                        "The value the referenced field must have "
                                        "for this field to be visible."
                                    ),
                                },
                            },
                            "required": ["field", "value"],
                        },
                    },
                    "required": ["id", "field_type", "label"],
                },
            },
            "submit_label": {
                "type": "string",
                "description": (
                    "Custom text for the submit button. "
                    "Use action-oriented labels: 'Cadastrar', 'Salvar', 'Confirmar'."
                ),
            },
            "cancel_label": {
                "type": "string",
                "description": "Custom text for the cancel button",
            },
            "size": {
                "type": "string",
                "enum": ["sm", "md", "lg", "xl"],
                "description": "Modal size hint (default: md)",
            },
        },
        "required": ["title", "fields"],
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """Build a dynamic FormInteraction and request user input.

        Uses ``REPLAN_AGENT`` mode — the tool does NOT block.  The
        ``InteractionReplanRequested`` exception propagates to
        ``BaseTool.run()``, which returns a ``ToolResult(status="interaction_replan")``.
        The agent framework later injects the user's responses as a
        ``[INTERACTION_RESPONSE]`` context block.
        """
        title: str = params.get("title", "Input Required")
        description: str = params.get("description", "")
        fields: list[dict[str, Any]] = params.get("fields", [])
        submit_label: str = params.get("submit_label", "")
        cancel_label: str = params.get("cancel_label", "")
        size: str = params.get("size", "md")

        # Validate minimum requirements
        if not fields:
            return self._failure(
                code="MISSING_PARAM",
                message="At least one field is required in 'fields'",
            )

        # Build the schema that FormInteraction will serialize
        schema_override: dict[str, Any] = {
            "interaction_type": "form",
            "fields": _normalise_fields(fields),
        }

        # Delegate to InteractionService in REPLAN_AGENT mode.
        # This raises InteractionReplanRequested, which BaseTool.run()
        # catches and converts to the appropriate ToolResult.
        await self.interaction.request(
            FormInteraction(
                title=title,
                description=description,
                schema_override=schema_override,
                submit_label=submit_label,
                cancel_label=cancel_label,
                size=size,
            ),
            resume=ResumeMode.REPLAN_AGENT,
            context={
                "tool": self.name,
                "field_count": len(fields),
            },
        )

        # Unreachable — InteractionReplanRequested is always raised above.
        return self._failure(
            code="UNEXPECTED",
            message="Interaction did not raise InteractionReplanRequested",
        )


def _normalise_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise agent-supplied field definitions into the Interaction DSL.

    Fills in defaults for optional properties and ensures each field has
    the minimum required keys for the FormRenderer to work correctly.
    """
    valid_types = {
        "text", "textarea", "number", "select",
        "checkbox", "checkbox_group", "radio", "date",
    }
    normalised: list[dict[str, Any]] = []
    for f in fields:
        ft = f.get("field_type", "text")
        if ft not in valid_types:
            ft = "text"

        entry: dict[str, Any] = {
            "id": f["id"],
            "field_type": ft,
            "label": f.get("label", f["id"].replace("_", " ").title()),
            "required": f.get("required", False),
            "value": f.get("value"),
            "placeholder": f.get("placeholder"),
            "hint": f.get("hint"),
            "description": f.get("description"),
            "default": f.get("default"),
            "example": f.get("example"),
            "visible": f.get("visible", True),
            "enabled": f.get("enabled", True),
        }

        # Type-specific extras
        if ft in ("text", "textarea"):
            entry["max_length"] = f.get("max_length")
        if ft == "text":
            entry["min_length"] = f.get("min_length")
        if ft == "textarea":
            entry["rows"] = f.get("rows", 4)
        if ft == "number":
            entry["min"] = f.get("min")
            entry["max"] = f.get("max")
            entry["step"] = f.get("step")
        if ft in ("select", "radio", "checkbox_group"):
            entry["options"] = f.get("options", [])
        if ft == "select":
            entry["multiple"] = f.get("multiple", False)
        if ft == "date":
            entry["min_date"] = f.get("min_date")
            entry["max_date"] = f.get("max_date")

        normalised.append(entry)

    return normalised
