"""Curator — Pydantic v2 request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import COLLECTION_STATUSES, COLLECTION_TYPES, ITEM_TYPES


# ── Collection ────────────────────────────────────────────────────────────────


class CollectionCreate(BaseModel):
    short_description: str = Field(..., max_length=100)
    description: str | None = None
    type: str
    subtype: str | None = Field(None, max_length=20)
    active: bool = True
    published: bool = Field(
        True,
        description=(
            "If False, the collection is hidden from public /data/ HTTP "
            "endpoints and its dump is skipped. Remains usable via admin API."
        ),
    )

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in COLLECTION_TYPES:
            raise ValueError(f"type must be one of: {', '.join(COLLECTION_TYPES)}")
        return v


class CollectionUpdate(BaseModel):
    short_description: str | None = Field(None, max_length=100)
    description: str | None = None
    active: bool | None = None
    published: bool | None = None


class CollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    short_description: str
    description: str | None
    slug: str
    type: str
    subtype: str | None
    created_at: datetime
    updated_at: datetime
    active: bool
    published: bool
    status: str
    item_count: int = 0


# ── Item ──────────────────────────────────────────────────────────────────────


class ItemAdd(BaseModel):
    value: str = Field(..., max_length=200, description="Raw value to add (IP, hash, domain, etc.)")
    type: str = Field(..., description="blocklist | allowlist | suspected")
    public_reference: str | None = Field(None, max_length=100)
    reference: str | None = Field(None, max_length=100)
    expire_days: int | None = Field(
        None,
        ge=1,
        description="Days until expiry. Omit for no expiry.",
    )

    @field_validator("type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        if v not in ITEM_TYPES:
            raise ValueError(f"type must be one of: {', '.join(ITEM_TYPES)}")
        return v


class ItemDelete(BaseModel):
    value: str = Field(..., max_length=200)
    type: str = Field(..., description="blocklist | allowlist | suspected")

    @field_validator("type")
    @classmethod
    def _check_item_type(cls, v: str) -> str:
        if v not in ITEM_TYPES:
            raise ValueError(f"type must be one of: {', '.join(ITEM_TYPES)}")
        return v


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    collection_id: int
    cidr: str | None
    value: str | None
    public_reference: str | None
    reference: str | None
    type: str
    created_at: datetime
    expire_at: datetime | None

    @field_validator("cidr", mode="before")
    @classmethod
    def _coerce_cidr(cls, v: object) -> str | None:
        """PostgreSQL CIDR columns are returned as IPv4Network/IPv6Network objects."""
        if v is None:
            return None
        return str(v)


# ── Paginated wrappers ────────────────────────────────────────────────────────


class PaginatedItems(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[ItemOut]
