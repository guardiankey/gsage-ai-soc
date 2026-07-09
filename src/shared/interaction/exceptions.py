"""gSage AI — Interaction Service exceptions."""

from __future__ import annotations

import uuid
from typing import Optional


class InteractionError(Exception):
    """Base exception for all interaction-related errors."""


class InteractionTimeout(InteractionError):
    """Raised when the user does not respond within the timeout window."""


class InteractionCancelled(InteractionError):
    """Raised when the user explicitly cancels the interaction."""


class InteractionReplanRequested(InteractionError):
    """Raised by ``InteractionService.request()`` when ``resume=REPLAN_AGENT``.

    The tool execution is aborted.  The agent framework catches this and
    injects the user's responses as a new ``[INTERACTION_RESPONSE]`` block
    so the agent can replan.

    Attributes:
        interaction_id: UUID of the persisted interaction record.
        schema: The serialized interaction schema (for audit).
        context: The audit context dict passed to ``request()``.
    """

    def __init__(
        self,
        interaction_id: uuid.UUID,
        schema: dict,
        context: Optional[dict] = None,
    ) -> None:
        self.interaction_id = interaction_id
        self.schema = schema
        self.context = context
        super().__init__(
            f"Interaction {interaction_id} requested REPLAN_AGENT — "
            "tool execution aborted; agent will replan with user responses."
        )
