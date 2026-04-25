"""Admin Console — service functions for Scheduled Jobs."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def list_jobs(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from src.shared.models.scheduled_job import GSageScheduledJob  # noqa: PLC0415

    result = await db.execute(
        select(GSageScheduledJob)
        .where(GSageScheduledJob.org_id == org_id)
        .order_by(GSageScheduledJob.name)
    )
    return [_job_to_dict(j) for j in result.scalars().all()]


async def toggle_job_active(
    db: AsyncSession,
    job_id: uuid.UUID,
) -> bool:
    from src.shared.models.scheduled_job import GSageScheduledJob  # noqa: PLC0415

    result = await db.execute(
        select(GSageScheduledJob).where(GSageScheduledJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        return False
    job.is_active = not job.is_active
    await db.commit()
    return True


async def list_background_tasks(
    db: AsyncSession,
    org_id: uuid.UUID,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from src.shared.models.background_task import GSageBackgroundTask  # noqa: PLC0415

    result = await db.execute(
        select(GSageBackgroundTask)
        .where(GSageBackgroundTask.org_id == org_id)
        .order_by(GSageBackgroundTask.created_at.desc())
        .limit(limit)
    )
    return [_task_to_dict(t) for t in result.scalars().all()]


def _job_to_dict(j: Any) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "name": j.name,
        "job_type": j.job_type,
        "cron_expression": j.cron_expression,
        "timezone": j.timezone,
        "is_active": j.is_active,
        "run_count": j.run_count,
        "last_run_at": j.last_run_at.isoformat() if j.last_run_at else "",
        "last_run_status": j.last_run_status or "",
        "redbeat_key": j.redbeat_key or "",
    }


def _task_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "tool_name": t.tool_name,
        "profile_id": t.profile_id,
        "status": t.status,
        "trigger": t.trigger,
        "celery_task_id": t.celery_task_id or "",
        "started_at": t.started_at.isoformat() if t.started_at else "",
        "completed_at": t.completed_at.isoformat() if t.completed_at else "",
        "error_message": t.error_message or "",
        "result": t.result or {},
    }
