"""gSage AI — Interaction API schemas (Pydantic)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class InteractionSubmitRequest(BaseModel):
    """Payload for ``POST /interactions/{id}/submit``."""

    responses: dict[str, Any] = Field(
        default_factory=dict,
        description="Form responses keyed by field ID, e.g. {'nome': 'João', 'idade': 30}",
    )


class InteractionCancelRequest(BaseModel):
    """Payload for ``POST /interactions/{id}/cancel`` (no body required)."""

    pass


class InteractionStatusResponse(BaseModel):
    """Response for submit/cancel endpoints."""

    interaction_id: str
    status: str


class InteractionPendingResponse(BaseModel):
    """Summary of a pending interaction (for polling / status checks)."""

    interaction_id: str
    interaction_type: str
    title: Optional[str] = None
    description: Optional[str] = None
    status: str
    resume_mode: str
    created_at: Optional[str] = None
