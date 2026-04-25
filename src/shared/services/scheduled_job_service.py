"""gSage AI — Scheduled Job Service (RedBeat + DB).

Provides helpers to:
  - sync_to_redbeat(job)       — create / update a RedBeat entry for a job
  - remove_from_redbeat(job)   — delete the RedBeat entry
  - sync_all_active_jobs()     — called at startup to reconcile DB ↔ Redis

All RedBeat keys follow the convention:
    redbeat:scheduled_job:<job_uuid>

which is stored back to job.redbeat_key so single-entry updates stay cheap.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

# ── Key convention ─────────────────────────────────────────────────────────

_TASK_NAME = "src.backend_api.app.tasks.scheduled_job.run_prompt_job"


def _key_for(job_id: uuid.UUID | str) -> str:
    return f"redbeat:scheduled_job:{job_id}"


# ── Cron parser ────────────────────────────────────────────────────────────

def _parse_cron(expr: str):
    """Convert a 5-field crontab string to a ``celery.schedules.crontab``."""
    from celery.schedules import crontab  # noqa: PLC0415

    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"cron_expression must have exactly 5 fields, got {len(parts)!r}: {expr!r}"
        )
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    return crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
    )


# ── Public API (sync, called from Celery/sync contexts) ───────────────────

def sync_to_redbeat(job) -> str:
    """Create or update a RedBeat entry for *job*.

    Returns the RedBeat key that was written.  Stores the key back on the
    in-memory ``job`` object (caller is responsible for persisting to DB).
    """
    from redbeat import RedBeatSchedulerEntry  # noqa: PLC0415
    from src.backend_api.app.celery_app import celery_app  # noqa: PLC0415

    key = _key_for(job.id)
    schedule = _parse_cron(job.cron_expression)
    kwargs = {"job_id": str(job.id)}

    try:
        # Try to load & update existing entry
        entry = RedBeatSchedulerEntry.from_key(key, app=celery_app)
        entry.schedule = schedule
        entry.kwargs = kwargs
        entry.enabled = bool(job.is_active)
        entry.save()
        logger.debug("RedBeat: updated entry %s (redis key: %s)", key, entry.key)
    except KeyError:
        # Entry doesn't exist yet — create it
        entry = RedBeatSchedulerEntry(
            name=key,
            task=_TASK_NAME,
            schedule=schedule,
            kwargs=kwargs,
            app=celery_app,
        )
        entry.save()
        logger.info("RedBeat: created entry %s (redis key: %s)", key, entry.key)

    # Store the ACTUAL Redis key (entry.key includes the redbeat_key_prefix).
    # Storing the computed `key` (name) would cause a key mismatch on removal.
    actual_key = entry.key
    job.redbeat_key = actual_key
    return actual_key


def remove_from_redbeat(job) -> None:
    """Delete the RedBeat entry for *job* if one exists."""
    from redbeat import RedBeatSchedulerEntry  # noqa: PLC0415
    from src.backend_api.app.celery_app import celery_app  # noqa: PLC0415

    key = job.redbeat_key or _key_for(job.id)
    logger.info("RedBeat: attempting to delete entry %s", key)
    try:
        entry = RedBeatSchedulerEntry.from_key(key, app=celery_app)
        entry.delete()
        logger.info("RedBeat: deleted entry %s", key)
    except KeyError:
        logger.warning("RedBeat: entry %s not found in Redis (already removed or key mismatch?)", key)
    except Exception as exc:
        logger.error("RedBeat: unexpected error deleting entry %s: %s", key, exc)
        raise

    job.redbeat_key = None


def remove_from_redbeat_by_id(job_id: uuid.UUID | str) -> None:
    """Delete a RedBeat entry by job UUID (when the job ORM object is unavailable)."""
    from redbeat import RedBeatSchedulerEntry  # noqa: PLC0415
    from src.backend_api.app.celery_app import celery_app  # noqa: PLC0415

    key = _key_for(job_id)
    try:
        entry = RedBeatSchedulerEntry.from_key(key, app=celery_app)
        entry.delete()
        logger.info("RedBeat: deleted orphan entry %s", key)
    except KeyError:
        pass


# ── Async startup sync ─────────────────────────────────────────────────────

async def sync_all_active_jobs(session_factory) -> None:
    """Load all is_active jobs from DB and ensure they exist in RedBeat.

    Called once at backend startup.  Handles the case where Redis was flushed
    (e.g. container restart) without touching the DB rows.

    ``session_factory`` must be an ``async_sessionmaker`` for the app DB.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from src.shared.models.scheduled_job import GSageScheduledJob  # noqa: PLC0415

    async with session_factory() as session:
        result = await session.execute(
            select(GSageScheduledJob).where(GSageScheduledJob.is_active.is_(True))
        )
        jobs = result.scalars().all()

    synced = 0
    errors = 0
    for job in jobs:
        try:
            key = sync_to_redbeat(job)
            # Persist the redbeat_key if it was missing
            if not job.redbeat_key:
                async with session_factory() as upd_session:
                    from sqlalchemy import update  # noqa: PLC0415
                    await upd_session.execute(
                        update(GSageScheduledJob)
                        .where(GSageScheduledJob.id == job.id)
                        .values(redbeat_key=key)
                    )
                    await upd_session.commit()
            synced += 1
        except Exception as exc:
            logger.error("sync_all_active_jobs: failed for job %s: %s", job.id, exc)
            errors += 1

    logger.info(
        "RedBeat startup sync complete: %d synced, %d errors (of %d active jobs)",
        synced,
        errors,
        len(jobs),
    )
