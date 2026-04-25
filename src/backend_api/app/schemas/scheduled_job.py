"""gSage AI — Scheduled Job schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ScheduledJobCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    job_type: str = Field(..., pattern="^(PROMPT_RUN|SYSTEM_TASK)$")
    cron_expression: str = Field(..., min_length=1, max_length=100)
    timezone: str = Field("UTC", max_length=64)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_active: bool = True
    max_runs: Optional[int] = Field(None, ge=1)
    # PROMPT_RUN fields
    prompt_content: Optional[str] = None
    prompt_conversation_id: Optional[uuid.UUID] = None
    prompt_output_format: str = Field("markdown", pattern="^(markdown|plain)$")
    # SYSTEM_TASK fields
    task_name: Optional[str] = Field(None, max_length=300)
    task_kwargs: Optional[dict[str, Any]] = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        parts = v.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"cron_expression must have exactly 5 fields, got {len(parts)}: {v!r}"
            )
        return v


class ScheduledJobUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    cron_expression: Optional[str] = Field(None, min_length=1, max_length=100)
    timezone: Optional[str] = Field(None, max_length=64)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_active: Optional[bool] = None
    max_runs: Optional[int] = Field(None, ge=1)
    prompt_content: Optional[str] = None
    prompt_conversation_id: Optional[uuid.UUID] = None
    prompt_output_format: Optional[str] = Field(None, pattern="^(markdown|plain)$")
    task_name: Optional[str] = Field(None, max_length=300)
    task_kwargs: Optional[dict[str, Any]] = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parts = v.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"cron_expression must have exactly 5 fields, got {len(parts)}: {v!r}"
            )
        return v


class ScheduledJobOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: Optional[str] = None
    job_type: str
    cron_expression: str
    timezone: str
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_active: bool
    max_runs: Optional[int] = None
    run_count: int
    prompt_content: Optional[str] = None
    prompt_conversation_id: Optional[uuid.UUID] = None
    prompt_output_format: str
    task_name: Optional[str] = None
    task_kwargs: Optional[dict[str, Any]] = None
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None
    last_run_result: Optional[dict[str, Any]] = None
    redbeat_key: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
