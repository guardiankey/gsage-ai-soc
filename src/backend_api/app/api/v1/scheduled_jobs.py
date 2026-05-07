"""gSage AI — Scheduled Jobs REST endpoints.

Routes
------
GET    /orgs/{org_id}/scheduled-jobs                   Paginated list (filters: job_type, is_active)
GET    /orgs/{org_id}/scheduled-jobs/{job_id}          Get single job
POST   /orgs/{org_id}/scheduled-jobs                   Create job + sync to RedBeat
PATCH  /orgs/{org_id}/scheduled-jobs/{job_id}          Update job + re-sync RedBeat
DELETE /orgs/{org_id}/scheduled-jobs/{job_id}          Delete job + remove from RedBeat
POST   /orgs/{org_id}/scheduled-jobs/{job_id}/activate   Set is_active=True + sync
POST   /orgs/{org_id}/scheduled-jobs/{job_id}/deactivate Set is_active=False + remove
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams, paginate_query
from src.backend_api.app.schemas.scheduled_job import (
    ScheduledJobCreate,
    ScheduledJobOut,
    ScheduledJobUpdate,
)
from src.shared.database import get_db
from src.shared.models.scheduled_job import GSageScheduledJob
from src.shared.services.scheduled_job_service import remove_from_redbeat, sync_to_redbeat

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_job_or_404(
    job_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> GSageScheduledJob:
    result = await db.execute(
        select(GSageScheduledJob).where(
            GSageScheduledJob.id == job_id,
            GSageScheduledJob.org_id == org_id,
            GSageScheduledJob.user_id == user_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled job not found")
    return job


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/scheduled-jobs",
    response_model=PaginatedResponse[ScheduledJobOut],
    summary="List scheduled jobs for the org",
)
async def list_scheduled_jobs(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends()],
    job_type: Optional[str] = Query(None, description="Filter by job_type: PROMPT_RUN or SYSTEM_TASK"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
) -> PaginatedResponse[ScheduledJobOut]:
    ctx.require_permission("scheduled_jobs:read")

    stmt = select(GSageScheduledJob).where(
        GSageScheduledJob.org_id == ctx.org_id,
        GSageScheduledJob.user_id == ctx.user_id,
    )
    if job_type is not None:
        stmt = stmt.where(GSageScheduledJob.job_type == job_type)
    if is_active is not None:
        stmt = stmt.where(GSageScheduledJob.is_active == is_active)
    stmt = stmt.order_by(GSageScheduledJob.created_at.desc())

    items, total = await paginate_query(db, stmt, pagination)
    return PaginatedResponse.build(
        [ScheduledJobOut.model_validate(j) for j in items],
        total=total,
        pagination=pagination,
    )


@router.get(
    "/orgs/{org_id}/scheduled-jobs/{job_id}",
    response_model=ScheduledJobOut,
    summary="Get a scheduled job by ID",
)
async def get_scheduled_job(
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduledJobOut:
    ctx.require_permission("scheduled_jobs:read")
    job = await _get_job_or_404(job_id, ctx.org_id, ctx.user_id, db)
    return ScheduledJobOut.model_validate(job)


@router.post(
    "/orgs/{org_id}/scheduled-jobs",
    response_model=ScheduledJobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new scheduled job",
)
async def create_scheduled_job(
    org_id: uuid.UUID,
    payload: ScheduledJobCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduledJobOut:
    ctx.require_permission("scheduled_jobs:write")

    job = GSageScheduledJob(
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        name=payload.name,
        description=payload.description,
        job_type=payload.job_type,
        cron_expression=payload.cron_expression,
        timezone=payload.timezone,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        is_active=payload.is_active,
        max_runs=payload.max_runs,
        prompt_content=payload.prompt_content,
        prompt_conversation_id=payload.prompt_conversation_id,
        prompt_output_format=payload.prompt_output_format,
        task_name=payload.task_name,
        task_kwargs=payload.task_kwargs,
    )
    db.add(job)
    await db.flush()  # get job.id before RedBeat sync

    if job.is_active:
        try:
            redbeat_key = await asyncio.to_thread(sync_to_redbeat, job)
            job.redbeat_key = redbeat_key
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to register job in scheduler: {exc}",
            ) from exc

    await db.commit()
    await db.refresh(job)
    return ScheduledJobOut.model_validate(job)


@router.patch(
    "/orgs/{org_id}/scheduled-jobs/{job_id}",
    response_model=ScheduledJobOut,
    summary="Update a scheduled job",
)
async def update_scheduled_job(
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    payload: ScheduledJobUpdate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduledJobOut:
    ctx.require_permission("scheduled_jobs:write")
    job = await _get_job_or_404(job_id, ctx.org_id, ctx.user_id, db)

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(job, field, value)

    # Re-sync RedBeat after any update
    if job.is_active:
        try:
            redbeat_key = await asyncio.to_thread(sync_to_redbeat, job)
            job.redbeat_key = redbeat_key
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to update job in scheduler: {exc}",
            ) from exc
    else:
        # Job is inactive — remove from RedBeat if it was there
        try:
            await asyncio.to_thread(remove_from_redbeat, job)
            job.redbeat_key = None
        except Exception as exc:
            logger.error(
                "update_scheduled_job: failed to remove RedBeat entry for job %s: %s",
                job_id, exc,
            )
            job.redbeat_key = None

    await db.commit()
    await db.refresh(job)
    return ScheduledJobOut.model_validate(job)


@router.delete(
    "/orgs/{org_id}/scheduled-jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a scheduled job",
)
async def delete_scheduled_job(
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    ctx.require_permission("scheduled_jobs:write")
    job = await _get_job_or_404(job_id, ctx.org_id, ctx.user_id, db)

    try:
        await asyncio.to_thread(remove_from_redbeat, job)
    except Exception:
        pass  # Proceed with deletion even if RedBeat removal fails

    await db.delete(job)
    await db.commit()


@router.post(
    "/orgs/{org_id}/scheduled-jobs/{job_id}/activate",
    response_model=ScheduledJobOut,
    summary="Activate a scheduled job",
)
async def activate_scheduled_job(
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduledJobOut:
    ctx.require_permission("scheduled_jobs:write")
    job = await _get_job_or_404(job_id, ctx.org_id, ctx.user_id, db)

    job.is_active = True
    try:
        redbeat_key = await asyncio.to_thread(sync_to_redbeat, job)
        job.redbeat_key = redbeat_key
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to register job in scheduler: {exc}",
        ) from exc

    await db.commit()
    await db.refresh(job)
    return ScheduledJobOut.model_validate(job)


@router.post(
    "/orgs/{org_id}/scheduled-jobs/{job_id}/deactivate",
    response_model=ScheduledJobOut,
    summary="Deactivate a scheduled job",
)
async def deactivate_scheduled_job(
    org_id: uuid.UUID,
    job_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduledJobOut:
    ctx.require_permission("scheduled_jobs:write")
    job = await _get_job_or_404(job_id, ctx.org_id, ctx.user_id, db)

    job.is_active = False
    try:
        await asyncio.to_thread(remove_from_redbeat, job)
        job.redbeat_key = None
    except Exception as exc:
        # Log the error but proceed — is_active=False in DB ensures the task
        # will skip at execution time even if the RedBeat entry lingers.
        logger.error(
            "deactivate_scheduled_job: failed to remove RedBeat entry for job %s: %s",
            job_id, exc,
        )
        job.redbeat_key = None

    await db.commit()
    await db.refresh(job)
    return ScheduledJobOut.model_validate(job)
