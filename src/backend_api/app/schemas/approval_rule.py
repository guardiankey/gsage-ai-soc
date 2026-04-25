"""gSage AI — Approval Rule schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ApprovalRuleCreate(BaseModel):
    tool_pattern: str = Field(..., max_length=200)
    user_id_pattern: str = Field("*", max_length=50)
    dept_id_pattern: str = Field("*", max_length=50)
    approver_user_id: uuid.UUID
    is_active: bool = True
    priority: int = Field(0, ge=0)
    description: Optional[str] = Field(None, max_length=2000)


class ApprovalRuleUpdate(BaseModel):
    tool_pattern: Optional[str] = Field(None, max_length=200)
    user_id_pattern: Optional[str] = Field(None, max_length=50)
    dept_id_pattern: Optional[str] = Field(None, max_length=50)
    approver_user_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None
    priority: Optional[int] = Field(None, ge=0)
    description: Optional[str] = Field(None, max_length=2000)


class ApprovalRuleOut(BaseModel):
    id: uuid.UUID
    org_id_pattern: str
    dept_id_pattern: str
    user_id_pattern: str
    tool_pattern: str
    approver_user_id: uuid.UUID
    is_active: bool
    priority: int
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
