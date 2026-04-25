"""gSage AI — Approvals (HITL) schemas."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ApprovalOut(BaseModel):
    """Serialised view of an Agno approval row."""

    id: str
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    status: Optional[str] = None
    approval_type: Optional[str] = None
    source_type: Optional[str] = None
    pause_type: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[dict[str, Any]] = None
    agent_id: Optional[str] = None
    user_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None
    requirements: Optional[list[dict[str, Any]] | dict[str, Any]] = None
    resolution_data: Optional[dict[str, Any]] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[int] = None
    expires_at: Optional[int] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

    # Delegation fields (populated when the approval was delegated via a rule)
    delegated_to_user_id: Optional[str] = None
    delegated_to_user_name: Optional[str] = None
    requester_user_name: Optional[str] = None
    summary: Optional[str] = None

    model_config = {"from_attributes": True}


class ApprovalListResponse(BaseModel):
    items: list[ApprovalOut]
    total: int
    page: int
    limit: int


class PendingCountResponse(BaseModel):
    count: int


class ApprovalResolve(BaseModel):
    action: Literal["approve", "reject"] = Field(
        ..., description="Whether to approve or reject the request"
    )
    comment: Optional[str] = Field(None, max_length=2000)
