"""gSage AI — Department schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Department schemas
# ---------------------------------------------------------------------------

class DepartmentCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name cannot be empty")
        if len(v) > 200:
            raise ValueError("name must be at most 200 characters")
        return v

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9-]{1,100}$", v):
            raise ValueError("slug must be 1–100 lowercase alphanumeric chars or hyphens")
        return v


class DepartmentUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name cannot be empty")
        if len(v) > 200:
            raise ValueError("name must be at most 200 characters")
        return v

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9-]{1,100}$", v):
            raise ValueError("slug must be 1–100 lowercase alphanumeric chars or hyphens")
        return v


class DepartmentOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str
    description: Optional[str]
    is_active: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Department member schemas
# ---------------------------------------------------------------------------

class DeptMemberAdd(BaseModel):
    user_id: uuid.UUID
    role: str = "member"

    @field_validator("role")
    @classmethod
    def role_valid(cls, v: str) -> str:
        if v not in ("admin", "member", "viewer"):
            raise ValueError("role must be admin, member, or viewer")
        return v


class DeptMemberUpdate(BaseModel):
    role: str
    is_active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def role_valid(cls, v: str) -> str:
        if v not in ("admin", "member", "viewer"):
            raise ValueError("role must be admin, member, or viewer")
        return v


class DeptMemberOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    dept_id: uuid.UUID
    role: str
    is_active: bool
    created_at: datetime

    # Enriched user info (joined in service layer)
    user_email: Optional[str] = None
    user_full_name: Optional[str] = None

    model_config = {"from_attributes": True}
