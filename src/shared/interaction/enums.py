"""gSage AI — Interaction Service enums."""

from __future__ import annotations

from enum import Enum


class ResumeMode(str, Enum):
    """Controls what happens after the user responds to an interaction."""

    CONTINUE_TOOL = "continue_tool"
    """Responses are returned to the tool; the tool continues ``execute()``."""

    REPLAN_AGENT = "replan_agent"
    """Responses are delivered to the agent as new input; the agent replans."""


class InteractionType(str, Enum):
    """Kinds of user interaction supported by the Interaction Service."""

    FORM = "form"
    CONFIRM = "confirm"          # future
    UPLOAD = "upload"            # future
    APPROVAL = "approval"        # future
    SELECTION = "selection"      # future
    NOTIFICATION = "notification"  # future


class InteractionStatus(str, Enum):
    """Lifecycle states of an interaction record."""

    WAITING_INPUT = "waiting_input"
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
