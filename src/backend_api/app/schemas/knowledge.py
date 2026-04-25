"""gSage AI — Knowledge schemas."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from src.backend_api.app.schemas.pagination import PaginatedResponse


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    max_results: int = Field(5, ge=1, le=50)


class KnowledgeSearchResult(BaseModel):
    id: str
    name: Optional[str] = None
    content: Optional[str] = None
    score: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeSearchResponse(BaseModel):
    results: list[KnowledgeSearchResult]
    total: int


class KnowledgeContentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    content: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = Field(None, max_length=1000)
    url: Optional[str] = Field(None, max_length=2000)
    metadata: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_content_or_url(self) -> "KnowledgeContentCreate":
        if not self.content and not self.url:
            raise ValueError("Either 'content' or 'url' must be provided")
        if self.url and not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ValueError("'url' must start with http:// or https://")
        return self


class KnowledgeContentOut(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    type: Optional[str] = None
    size: Optional[int] = None
    status: Optional[str] = None
    linked_to: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

    model_config = {"from_attributes": True}


class KnowledgeContentListResponse(PaginatedResponse[KnowledgeContentOut]):
    """Backward-compatible alias for ``PaginatedResponse[KnowledgeContentOut]``."""


# ---------------------------------------------------------------------------
# Ingest job schemas
# ---------------------------------------------------------------------------

class IngestJobSubmitResponse(BaseModel):
    """Returned immediately (HTTP 202) after a document is accepted for ingest."""

    job_id: str
    status: str
    filename: str
    scope: str


class IngestJobStatusResponse(BaseModel):
    """Full status of an ingest job (returned by GET /knowledge/ingest/{job_id})."""

    job_id: str
    status: str
    filename: str
    scope: str
    file_size: int
    chunks_stored: Optional[int] = None
    error_message: Optional[str] = None
    storage_key: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = {"from_attributes": True}
