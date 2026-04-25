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

