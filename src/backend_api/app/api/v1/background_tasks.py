"""gSage AI — Background Tasks API routes.

Routes
------
- ``GET /orgs/{org_id}/background-tasks``       — paginated list (filterable)
- ``GET /orgs/{org_id}/background-tasks/{id}``  — detail
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams, paginate_query
from src.shared.database import get_db
from src.shared.models.background_task import GSageBackgroundTask

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BackgroundTaskOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    user_id: Optional[uuid.UUID]
    gsage_session_id: Optional[uuid.UUID]
    tool_name: str
    profile_id: str
    trigger: str
    status: str
    celery_task_id: Optional[str]
    result: Optional[dict]
    error_message: Optional[str]
    notified: bool
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/background-tasks",
    response_model=PaginatedResponse[BackgroundTaskOut],
    summary="List background tool execution tasks",
)
async def list_background_tasks(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends()],
    tool_name: Optional[str] = Query(default=None, description="Filter by tool name"),
    task_status: Optional[str] = Query(default=None, alias="status", description="Filter by status"),
    session_id: Optional[uuid.UUID] = Query(default=None, description="Filter by gSage session (conversation) ID"),
) -> PaginatedResponse[BackgroundTaskOut]:
    """List background tool executions for the organisation.

    Requires ``agents:run`` permission (same as sending a chat message).
    """
    ctx.require_permission("agents:run")

    stmt = (
        select(GSageBackgroundTask)
        .where(GSageBackgroundTask.org_id == ctx.org_id)
        .order_by(GSageBackgroundTask.created_at.desc())
    )

    if tool_name:
        stmt = stmt.where(GSageBackgroundTask.tool_name == tool_name)
    if task_status:
        stmt = stmt.where(GSageBackgroundTask.status == task_status)
    if session_id:
        stmt = stmt.where(GSageBackgroundTask.gsage_session_id == session_id)

    tasks, total = await paginate_query(db, stmt, pagination)
    return PaginatedResponse.build(
        [BackgroundTaskOut.model_validate(t) for t in tasks],
        total=total,
        pagination=pagination,
    )


@router.get(
    "/orgs/{org_id}/background-tasks/{task_id}",
    response_model=BackgroundTaskOut,
    summary="Get a background task by ID",
)
async def get_background_task(
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BackgroundTaskOut:
    """Return a single background task. Returns 404 if not found or not owned by org."""
    ctx.require_permission("agents:run")

    result = await db.execute(
        select(GSageBackgroundTask).where(
            GSageBackgroundTask.id == task_id,
            GSageBackgroundTask.org_id == ctx.org_id,
        )
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Background task not found",
        )
    return BackgroundTaskOut.model_validate(task)
