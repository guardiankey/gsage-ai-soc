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
    """Return finished (completed or failed), un-notified background tasks for the session."""
    result = await db.execute(
        select(GSageBackgroundTask).where(
            GSageBackgroundTask.gsage_session_id == gsage_session_id,
            GSageBackgroundTask.status.in_([
                BackgroundTaskStatus.COMPLETED,
                BackgroundTaskStatus.FAILED,
            ]),
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


_BG_RESULT_TRUNCATE_BYTES = 8000
# Top-level keys that we always preserve uncut after the bulk truncation, so the
# agent never loses references to downloadable artifacts when the row data is
# large. Keep this list short and agent-relevant.
_BG_RESULT_PRESERVE_KEYS = (
    "artifacts",
    "file",
    "files",
    "rows_total",
    "rows_overflow",
    "rows_preview_limit",
    "total_count",
    "truncated",
)


def build_bg_context_block(tasks: list[GSageBackgroundTask]) -> str:
    """Build the [BACKGROUND_TASKS_COMPLETED] injection block.

    The ``data`` payload of each finished task is JSON-serialized and clipped
    to :data:`_BG_RESULT_TRUNCATE_BYTES` characters to keep the prompt small.
    Keys listed in :data:`_BG_RESULT_PRESERVE_KEYS` (artifact descriptors,
    overflow flags, totals) are appended uncut after the truncated block so
    the agent never loses the download links / counts when row data is large.
    """
    lines = ["[BACKGROUND_TASKS_COMPLETED]"]
    for task in tasks:
        lines.append(f"- task_id={task.id} | tool={task.tool_name}")
        if task.result:
            data = task.result.get("data") or {}
            summary = json.dumps(data, ensure_ascii=False, default=str)
            if len(summary) > _BG_RESULT_TRUNCATE_BYTES:
                summary = summary[:_BG_RESULT_TRUNCATE_BYTES] + "... [truncated]"
            lines.append(f"  result_data: {summary}")
            # Re-emit small, agent-critical keys uncut so they survive any
            # truncation that may have hidden them above.
            if isinstance(data, dict):
                preserved: dict = {
                    k: data[k] for k in _BG_RESULT_PRESERVE_KEYS if k in data
                }
                if preserved:
                    lines.append(
                        "  result_meta: "
                        + json.dumps(preserved, ensure_ascii=False, default=str)
                    )
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


async def resolve_user_active_dept_id(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Optional[uuid.UUID]:
    """Resolve the active department for a user within an org.

    Resolution order:
        1. ``user.default_dept_id`` — when set AND the user still has an
           active membership in that dept (and the dept belongs to ``org_id``
           and is active).
        2. The first active department membership the user has in the org,
           preferring the org's "Default" dept (``is_default=True``).

    Returns ``None`` only if the user has no active department membership
    in the given org.
    """
    from src.shared.models.department import GSageDepartment  # noqa: PLC0415
    from src.shared.models.user import GSageUser  # noqa: PLC0415
    from src.shared.models.user_department import GSageUserDepartment  # noqa: PLC0415

    # 1. User-profile preferred default
    user_row = (
        await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    ).scalar_one_or_none()
    preferred_dept_id = getattr(user_row, "default_dept_id", None) if user_row else None

    if preferred_dept_id is not None:
        membership = (
            await db.execute(
                select(GSageDepartment)
                .join(
                    GSageUserDepartment,
                    GSageUserDepartment.dept_id == GSageDepartment.id,
                )
                .where(
                    GSageDepartment.id == preferred_dept_id,
                    GSageDepartment.org_id == org_id,
                    GSageDepartment.is_active.is_(True),
                    GSageUserDepartment.user_id == user_id,
                    GSageUserDepartment.is_active.is_(True),
                )
            )
        ).scalars().first()
        if membership is not None:
            return membership.id

    # 2. Fallback: first active membership, preferring org default
    fallback = (
        await db.execute(
            select(GSageDepartment)
            .join(
                GSageUserDepartment,
                GSageUserDepartment.dept_id == GSageDepartment.id,
            )
            .where(
                GSageUserDepartment.user_id == user_id,
                GSageUserDepartment.is_active.is_(True),
                GSageDepartment.org_id == org_id,
                GSageDepartment.is_active.is_(True),
            )
            .order_by(GSageDepartment.is_default.desc())
        )
    ).scalars().first()
    return fallback.id if fallback else None
