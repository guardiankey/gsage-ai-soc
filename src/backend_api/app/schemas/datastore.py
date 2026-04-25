"""gSage AI — DataStore Pydantic schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Store schemas
# ---------------------------------------------------------------------------


class DataStoreCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    json_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")
    visibility: str = Field("shared", pattern="^(private|shared)$")
    max_records: Optional[int] = Field(None, ge=1)


class DataStoreUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    json_schema: Optional[dict[str, Any]] = Field(None, alias="schema")
    visibility: Optional[str] = Field(None, pattern="^(private|shared)$")
    max_records: Optional[int] = Field(None, ge=1)
    is_active: Optional[bool] = None


class DataStoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    org_id: uuid.UUID
    created_by: Optional[uuid.UUID] = None
    name: str
    description: Optional[str] = None
    json_schema: dict[str, Any] = Field(alias="schema")
    visibility: str
    max_records: int
    record_count: int
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Record schemas
# ---------------------------------------------------------------------------


class DataStoreRecordCreate(BaseModel):
    data: dict[str, Any]


class DataStoreRecordBulkCreate(BaseModel):
    records: list[dict[str, Any]] = Field(..., min_length=1)


class DataStoreRecordUpdate(BaseModel):
    data: dict[str, Any]


class DataStoreRecordOut(BaseModel):
    id: uuid.UUID
    datastore_id: uuid.UUID
    data: dict[str, Any]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class DataStoreRecordQueryParams(BaseModel):
    filters: Optional[dict[str, Any]] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)
