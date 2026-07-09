"""gSage AI — Interaction Service data models (dataclasses, not DB)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InteractionRequest:
    """Internal representation of an interaction request before persistence."""

    interaction_type: str
    title: str
    description: str = ""
    schema: dict = field(default_factory=dict)
    resume_mode: str = "continue_tool"
    timeout_seconds: int = 600
    context: Optional[dict] = None


@dataclass
class InteractionResponse:
    """Internal representation of a user's response to an interaction."""

    interaction_id: uuid.UUID
    status: str  # "submitted" | "cancelled" | "timeout"
    data: dict = field(default_factory=dict)
    context: Optional[dict] = None
