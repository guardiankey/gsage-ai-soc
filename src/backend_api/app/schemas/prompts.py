"""gSage AI — Prompt Library Pydantic schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Prompt Category
# ---------------------------------------------------------------------------


class PromptCategoryCreate(BaseModel):
    """Payload to create a prompt category."""

    name: str = Field(min_length=1, max_length=100)
    parent_id: Optional[uuid.UUID] = None
    dept_id: Optional[uuid.UUID] = Field(
        None,
        description="NULL = org-level category, UUID = department-level",
    )
    description: Optional[str] = Field(None, max_length=2000)


class PromptCategoryUpdate(BaseModel):
    """Payload to update a prompt category."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    parent_id: Optional[uuid.UUID] = None
    description: Optional[str] = Field(None, max_length=2000)
    is_active: Optional[bool] = None


class PromptCategoryOut(BaseModel):
    """Prompt category as returned by the API."""

    id: uuid.UUID
    name: str
    parent_id: Optional[uuid.UUID]
    dept_id: Optional[uuid.UUID]
    description: Optional[str]
    sort_order: int
    is_active: bool
    children: list[PromptCategoryOut] = Field(default_factory=list)
    prompt_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_Scope = Literal["personal", "department", "organization"]


class PromptCreate(BaseModel):
    """Payload to create a prompt."""

    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=50000)
    description: Optional[str] = Field(None, max_length=500)
    category_id: Optional[uuid.UUID] = None
    scope: _Scope = Field(
        "personal",
        description="personal | department | organization",
    )


class PromptUpdate(BaseModel):
    """Payload to update a prompt."""

    title: Optional[str] = Field(None, min_length=1, max_length=255)
    content: Optional[str] = Field(None, min_length=1, max_length=50000)
    description: Optional[str] = Field(None, max_length=500)
    category_id: Optional[uuid.UUID] = None
    scope: Optional[_Scope] = None
    is_active: Optional[bool] = None


class PromptOut(BaseModel):
    """Prompt as returned by the API.

    The ``content`` field is only included when the user has access to
    the prompt (scope visibility rules apply). When listing prompts for
    the modal, content may be omitted to reduce payload size.
    """

    id: uuid.UUID
    title: str
    description: Optional[str]
    content: str = ""
    scope: str
    category_id: Optional[uuid.UUID]
    category_name: Optional[str] = None
    created_by: uuid.UUID
    creator_name: str = ""
    is_favorite: bool = False
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PromptListResponse(BaseModel):
    """Paginated list of prompts."""

    prompts: list[PromptOut]
    total: int
    page: int
    page_size: int


class PromptSearchRequest(BaseModel):
    """Search / filter parameters for prompts."""

    query: Optional[str] = Field(None, min_length=1, max_length=500)
    scope: Optional[_Scope] = None
    category_id: Optional[uuid.UUID] = None
    favorites_only: bool = False
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)
