"""gSage AI — Maintenance Celery tasks.

Tasks
-----
cleanup_inactive_sessions
    Deletes Agno sessions that have been idle for more than
    ``SESSION_IDLE_DAYS`` days.  Runs hourly via Celery Beat.

prune_es_trace_indices
    Deletes Elasticsearch trace indices older than the configured
    retention period.  Runs daily via Celery Beat.

purge_expired_files
    Deletes expired tool-generated file objects from MinIO and marks the corresponding
    DB records with ``purged_at``.  Runs every hour via Celery Beat.

reap_orphan_background_tasks
    Marks as FAILED any background task that has been in RUNNING state for
    longer than the per-tool background timeout (or a global cap when the
    tool can't be resolved).  Catches tasks orphaned by celery-tools worker
    crashes / restarts.  Runs every 5 minutes via Celery Beat.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from src.backend_api.app.celery_app import celery_app

log = logging.getLogger(__name__)

# Sessions idle longer than this many seconds will be purged
_SESSION_IDLE_SECONDS = 7 * 24 * 3600  # 7 days


@celery_app.task(name="src.backend_api.app.tasks.maintenance.cleanup_inactive_sessions")
def cleanup_inactive_sessions() -> dict:
    """Remove Agno sessions that have been idle for over 7 days.

    Returns a summary dict with ``deleted`` count.
    """
    import asyncio

    from src.backend_api.app.services.agent_factory import get_agno_db

    async def _run() -> int:
        agno_db = get_agno_db()
        cutoff = int(time.time()) - _SESSION_IDLE_SECONDS
        sessions, _ = await agno_db.get_sessions(limit=1000, deserialize=False)  # type: ignore[misc]

        deleted = 0
        for session in sessions:  # type: ignore[union-attr]
            updated_at = session.get("updated_at") or session.get("created_at") or 0
            if updated_at < cutoff:
                session_id = session.get("session_id") or session.get("id")
                if session_id:
                    await agno_db.delete_session(session_id)
                    deleted += 1

        return deleted

    try:
        deleted = asyncio.run(_run())
        log.info("cleanup_inactive_sessions: deleted %d sessions", deleted)
        return {"deleted": deleted, "status": "ok"}
    except Exception as exc:
        log.error("cleanup_inactive_sessions failed: %s", exc, exc_info=True)
        return {"deleted": 0, "status": "error", "detail": str(exc)}


@celery_app.task(name="src.backend_api.app.tasks.maintenance.prune_es_trace_indices")
def prune_es_trace_indices() -> dict:
    """Delete Elasticsearch trace indices older than the configured retention period.

    Uses the ``elasticsearch_trace_index_prefix`` and
    ``elasticsearch_trace_retention_days`` settings.  Runs daily via Celery Beat.
    """
    import asyncio

    async def _run() -> int:
        from elasticsearch import AsyncElasticsearch

        from src.shared.config.settings import get_settings

        settings = get_settings()
        prefix = f"{settings.elasticsearch_trace_index_prefix}agno-traces-"
        retention_days = settings.elasticsearch_trace_retention_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        es = AsyncElasticsearch(
            hosts=[settings.elasticsearch_url],
            request_timeout=10.0,
        )
        deleted = 0
        try:
            wildcard = f"{prefix}*"
            response = await es.cat.indices(index=wildcard, format="json", h="index")

            for idx in response:
                index_name: str = idx.get("index", "")  # type: ignore[union-attr]
                date_str = index_name.replace(prefix, "")
                try:
                    index_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    if index_date < cutoff:
                        await es.indices.delete(index=index_name)
                        log.info("Pruned ES trace index: %s", index_name)
                        deleted += 1
                except ValueError:
                    continue
        finally:
            await es.close()

        return deleted

    try:
        deleted = asyncio.run(_run())
        log.info("prune_es_trace_indices: removed %d indices", deleted)
        return {"deleted": deleted, "status": "ok"}
    except Exception as exc:
        log.error("prune_es_trace_indices failed: %s", exc, exc_info=True)
        return {"deleted": 0, "status": "error", "detail": str(exc)}


@celery_app.task(name="src.backend_api.app.tasks.maintenance.purge_expired_files")
def purge_expired_files() -> dict:
    """Delete expired tool-generated file objects from MinIO and mark DB records as purged.

    Runs hourly via Celery Beat.  The DB row is never deleted — only the
    MinIO object (bytes) is removed and ``purged_at`` is set on the row.

    Returns a summary dict with ``purged`` count.
    """
    import asyncio

    async def _run() -> int:
        from datetime import datetime, timezone

        from sqlalchemy import select, update
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        from src.shared.config.settings import get_settings
        from src.shared.models.generated_file import GSageFile
        from src.shared.services.file_store import get_file_store

        settings = get_settings()
        engine = create_async_engine(settings.database_url, echo=False)
        async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        now = datetime.now(timezone.utc)
        purged = 0

        async with async_session() as session:
            # Fetch rows that have expired and have not been purged yet.
            # Templates always have expires_at=NULL so they are excluded by
            # the first condition, but we add an explicit guard as safety net.
            stmt = select(GSageFile).where(
                GSageFile.expires_at <= now,
                GSageFile.purged_at.is_(None),
                GSageFile.category != "template",
            )
            rows = (await session.execute(stmt)).scalars().all()

            if not rows:
                return 0

            store = get_file_store()

            for row in rows:
                try:
                    await store.delete_object(row.storage_key, category=row.category)
                    row.purged_at = now
                    purged += 1
                    log.info(
                        "purge_expired_files: purged %s (key=%s)",
                        row.id, row.storage_key,
                    )
                except Exception as exc:
                    log.error(
                        "purge_expired_files: failed to purge %s: %s",
                        row.id, exc,
                    )

            await session.commit()

        await engine.dispose()
        return purged

    try:
        purged = asyncio.run(_run())
        log.info("purge_expired_files: purged %d files", purged)
        return {"purged": purged, "status": "ok"}
    except Exception as exc:
        log.error("purge_expired_files failed: %s", exc, exc_info=True)
        return {"purged": 0, "status": "error", "detail": str(exc)}


# Hard upper bound for any single background task, used as fallback when the
# tool cannot be resolved (e.g. tool was removed from the registry) or when
# ``started_at`` is missing.  Comfortably above the longest known
# ``background_timeout_seconds`` (1800 s for E-goi paging tools).
_ORPHAN_REAP_FALLBACK_SECONDS = 7200  # 2 hours
# Extra slack added to the resolved per-tool timeout before declaring a task
# orphaned.  Covers clock drift and the time spent in result/audit persistence
# AFTER ``execute()`` returns.
_ORPHAN_REAP_GRACE_SECONDS = 120


@celery_app.task(name="src.backend_api.app.tasks.maintenance.reap_orphan_background_tasks")
def reap_orphan_background_tasks() -> dict:
    """Mark stuck RUNNING background tasks as FAILED.

    A task is considered orphaned when its elapsed time since ``started_at``
    exceeds the per-tool ``background_timeout_seconds`` (or
    ``timeout_seconds * 3`` legacy heuristic) plus a grace period.  Tasks
    without ``started_at`` are reaped using the fallback cap measured from
    ``created_at``.

    Typical cause: the celery-tools worker was restarted / crashed mid-run,
    leaving the DB row in RUNNING state forever.  Without this reaper the
    chat would never receive a ``[BACKGROUND_TASKS_COMPLETED]`` notification
    for that task, and the agent would keep telling the user it is "still
    running".
    """
    import asyncio

    async def _run() -> dict:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        from src.shared.config.settings import get_settings
        from src.shared.models.background_task import (
            BackgroundTaskStatus,
            GSageBackgroundTask,
        )

        # Lazy registry build so that maintenance imports stay light when
        # the worker is the backend (no MCP tools installed) — fall back to
        # the global cap if the registry can't be loaded.
        try:
            from src.mcp_server.registry.registry import build_registry  # noqa: PLC0415

            tool_registry = build_registry()
        except Exception:  # noqa: BLE001
            log.warning(
                "reap_orphan_background_tasks: tool registry unavailable, "
                "using fallback cap of %ss", _ORPHAN_REAP_FALLBACK_SECONDS,
            )
            tool_registry = None

        settings = get_settings()
        engine = create_async_engine(settings.database_url, echo=False)
        async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        now = datetime.now(timezone.utc)
        reaped = 0

        async with async_session() as session:
            stmt = select(GSageBackgroundTask).where(
                GSageBackgroundTask.status == BackgroundTaskStatus.RUNNING,
            )
            rows = (await session.execute(stmt)).scalars().all()

            for row in rows:
                # Resolve per-tool timeout when possible.
                tool = (
                    tool_registry.get_tool(row.tool_name)
                    if tool_registry is not None
                    else None
                )
                if tool is not None:
                    bg_timeout = (
                        tool.background_timeout_seconds
                        if tool.background_timeout_seconds is not None
                        else tool.timeout_seconds * 3
                    )
                else:
                    bg_timeout = _ORPHAN_REAP_FALLBACK_SECONDS

                deadline_seconds = bg_timeout + _ORPHAN_REAP_GRACE_SECONDS
                # Use started_at when available, else fall back to created_at
                # (covers tasks that crashed before flipping to RUNNING but
                # somehow ended up with that status).
                anchor = row.started_at or row.created_at
                if anchor is None:
                    continue
                elapsed = (now - anchor).total_seconds()
                if elapsed < deadline_seconds:
                    continue

                row.status = BackgroundTaskStatus.FAILED
                row.error_message = (
                    f"[ORPHAN_REAPED] Task stuck in RUNNING for {int(elapsed)}s "
                    f"(deadline {deadline_seconds}s). Likely caused by a worker "
                    f"restart or crash."
                )[:2000]
                row.completed_at = now
                reaped += 1
                log.warning(
                    "reap_orphan_background_tasks: reaped task %s (tool=%s "
                    "elapsed=%ds deadline=%ds)",
                    row.id, row.tool_name, int(elapsed), deadline_seconds,
                )

            if reaped:
                await session.commit()

        await engine.dispose()
        return {"reaped": reaped, "scanned": len(rows)}

    try:
        result = asyncio.run(_run())
        if result["reaped"]:
            log.info(
                "reap_orphan_background_tasks: reaped %d / %d running tasks",
                result["reaped"], result["scanned"],
            )
        return {"status": "ok", **result}
    except Exception as exc:
        log.error("reap_orphan_background_tasks failed: %s", exc, exc_info=True)
        return {"status": "error", "reaped": 0, "detail": str(exc)}

