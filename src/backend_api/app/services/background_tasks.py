"""Background task notification and department context helpers.

Shared between the HTTP chat endpoint and async channel workers (Telegram, etc.)
so that pending background task results and department context can be injected
into the agent context before the next agent run.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.background_task import BackgroundTaskStatus, GSageBackgroundTask

log = logging.getLogger(__name__)


async def get_pending_bg_notifications(
    gsage_session_id: uuid.UUID,
    db: AsyncSession,
) -> list[GSageBackgroundTask]:
    """Return completed, un-notified background tasks for the given session."""
    result = await db.execute(
        select(GSageBackgroundTask).where(
            GSageBackgroundTask.gsage_session_id == gsage_session_id,
            GSageBackgroundTask.status == BackgroundTaskStatus.COMPLETED,
            GSageBackgroundTask.notified.is_(False),
        ).order_by(GSageBackgroundTask.completed_at.asc())
    )
    return list(result.scalars().all())


async def has_active_bg_tasks(
    gsage_session_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    """Return True if the session has any QUEUED or RUNNING background tasks."""
    from sqlalchemy import exists
    stmt = select(
        exists().where(
            GSageBackgroundTask.gsage_session_id == gsage_session_id,
            GSageBackgroundTask.status.in_([
                BackgroundTaskStatus.QUEUED,
                BackgroundTaskStatus.RUNNING,
            ]),
        )
    )
    result = await db.execute(stmt)
    return bool(result.scalar())


def build_bg_context_block(tasks: list[GSageBackgroundTask]) -> str:
    """Build the [BACKGROUND_TASKS_COMPLETED] injection block."""
    lines = ["[BACKGROUND_TASKS_COMPLETED]"]
    for task in tasks:
        lines.append(f"- task_id={task.id} | tool={task.tool_name}")
        if task.result:
            data = task.result.get("data") or {}
            summary = json.dumps(data, ensure_ascii=False, default=str)
            if len(summary) > 8000:
                summary = summary[:8000] + "... [truncated]"
            lines.append(f"  result_data: {summary}")
        if task.error_message:
            lines.append(f"  error: {task.error_message[:200]}")
    lines.append("[/BACKGROUND_TASKS_COMPLETED]")
    return "\n".join(lines)


async def mark_bg_tasks_notified(
    task_ids: list[uuid.UUID],
    db: AsyncSession,
) -> None:
    """Mark background tasks as notified (idempotent, best-effort).

    Does NOT commit — the caller is responsible for transaction management.
    When called inside a ``session.begin()`` context the outer transaction
    handles the commit.  When called outside a managed transaction the caller
    must issue ``await db.commit()`` after this function returns.
    """
    try:
        await db.execute(
            update(GSageBackgroundTask)
            .where(GSageBackgroundTask.id.in_(task_ids))
            .values(notified=True)
        )
    except Exception as exc:
        log.warning("Failed to mark bg tasks notified: %s", exc)


async def load_dept_name(dept_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    """Return the department name for a given dept_id, or None on any error."""
    try:
        from src.shared.models.department import GSageDepartment  # noqa: PLC0415
        result = await db.execute(
            select(GSageDepartment).where(GSageDepartment.id == dept_id)
        )
        dept = result.scalar_one_or_none()
        return dept.name if dept else None
    except Exception:
        return None


def build_dept_context_block(dept_id: uuid.UUID, dept_name: Optional[str]) -> str:
    """Build the [DEPARTMENT_CONTEXT] injection block for the agent message."""
    name_str = dept_name or str(dept_id)
    return (
        "[DEPARTMENT_CONTEXT]\n"
        f"The user's active department is: {name_str} (ID: {dept_id})\n"
        "All department-scoped operations (datastores, files, tool configs) "
        "must use this department context automatically. "
        "Do NOT ask the user to define a department.\n"
        "[/DEPARTMENT_CONTEXT]"
    )
