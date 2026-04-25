"""gSage AI — Generic pagination primitives.

Usage (FastAPI route)::

    from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams

    @router.get("/items", response_model=PaginatedResponse[ItemOut])
    async def list_items(
        org_id: uuid.UUID,
        ctx: Annotated[TenantContext, Depends(get_tenant_context)],
        pagination: Annotated[PaginationParams, Depends()],
    ) -> PaginatedResponse[ItemOut]:
        rows, total = await db_call(limit=pagination.limit, page=pagination.page)
        return PaginatedResponse.build(rows, total=total, pagination=pagination)

Usage (SQLAlchemy)::

    from src.backend_api.app.schemas.pagination import paginate_query

    rows, total = await paginate_query(db, stmt, pagination)
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

T = TypeVar("T")


class PaginationParams:
    """FastAPI dependency — extracts and validates ``page`` + ``limit`` query params.

    Example query string: ``?page=2&limit=50``

    The ``offset`` property converts to SQLAlchemy-friendly row offset automatically.
    """

    def __init__(
        self,
        page: Annotated[int, Query(ge=1, description="Page number (1-based)")] = 1,
        limit: Annotated[
            int, Query(ge=1, le=100, description="Items per page (max 100)")
        ] = 20,
    ) -> None:
        self.page = page
        self.limit = limit

    @property
    def offset(self) -> int:
        """Row offset for SQL OFFSET clause."""
        return (self.page - 1) * self.limit


class PaginatedResponse(BaseModel, Generic[T]):
    """Standard paginated list envelope.

    All list endpoints should return this schema so that clients can rely
    on a consistent structure for pagination navigation.

    Fields:
        items:    Items on the current page.
        total:    Total number of items matching the query (across all pages).
        page:     Current page number (1-based).
        limit:    Maximum items per page requested.
        has_more: True if there are additional pages after the current one.
    """

    items: list[T]
    total: int = Field(description="Total items matching the query (all pages)")
    page: int = Field(description="Current page number (1-based)")
    limit: int = Field(description="Max items per page")
    has_more: bool = Field(description="True if there are more pages after this one")

    @classmethod
    def build(
        cls,
        items: list[T],
        *,
        total: int,
        pagination: PaginationParams,
    ) -> "PaginatedResponse[T]":
        """Convenience constructor that derives ``has_more`` automatically."""
        return cls(
            items=items,
            total=total,
            page=pagination.page,
            limit=pagination.limit,
            has_more=(pagination.page * pagination.limit < total),
        )


async def paginate_query(
    db: AsyncSession,
    stmt: Select,
    pagination: PaginationParams,
) -> tuple[list, int]:
    """Execute a SQLAlchemy SELECT with OFFSET/LIMIT and return ``(rows, total_count)``.

    The ``total_count`` is obtained via a ``SELECT COUNT(*)`` over a subquery —
    a single round-trip that avoids fetching all rows.

    Args:
        db:         Async SQLAlchemy session.
        stmt:       Base SELECT statement (without OFFSET/LIMIT).
        pagination: ``PaginationParams`` dependency instance.

    Returns:
        Tuple of (list of ORM objects, total count as int).
    """
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = (await db.execute(count_stmt)).scalar() or 0

    paginated_stmt = stmt.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(paginated_stmt)
    return list(result.scalars().all()), total
