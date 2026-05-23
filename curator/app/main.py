"""Curator — FastAPI application entry point.

Lifecycle:
    - Creates database tables (create_all) on startup.
    - Runs seed collections.
    - Starts a periodic background task for expired item cleanup (soft-delete).
    - Starts a periodic background task that physically purges items soft-deleted
      more than DIFF_RETENTION_DAYS ago.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from sqlalchemy import delete, select, text, update

from .config import DIFF_RETENTION_DAYS, get_settings
from .data_routes import router as data_router
from .database import get_engine, get_session_factory
from .dump import dump_collection
from .models import Base, Collection, Item
from .routes import router as admin_router
from .seed import run_seed

log = logging.getLogger(__name__)

_cleanup_task: asyncio.Task | None = None
_purge_task: asyncio.Task | None = None


async def _expiry_cleanup_loop() -> None:
    """Soft-delete expired items and re-dump affected collections periodically."""
    settings = get_settings()
    factory = get_session_factory()

    while True:
        await asyncio.sleep(settings.expiry_check_interval)
        try:
            async with factory() as session:
                now = datetime.now(tz=timezone.utc)

                # Find collections that have at least one expired, not-yet-soft-deleted item
                result = await session.execute(
                    select(Item.collection_id)
                    .where(Item.expire_at.isnot(None))
                    .where(Item.expire_at <= now)
                    .where(Item.deleted_at.is_(None))
                    .distinct()
                )
                affected_ids = [row[0] for row in result.all()]

                if not affected_ids:
                    log.debug("expiry_cleanup: no expired items")
                    continue

                # Soft-delete expired items so the differential history is preserved.
                await session.execute(
                    update(Item)
                    .where(Item.expire_at.isnot(None))
                    .where(Item.expire_at <= now)
                    .where(Item.deleted_at.is_(None))
                    .values(deleted_at=now)
                )

                # Mark affected collections as waiting
                for cid in affected_ids:
                    c = await session.get(Collection, cid)
                    if c and c.status == "idle":
                        c.status = "waiting"
                        c.touch()

                await session.commit()
                log.info("expiry_cleanup: soft-deleted expired items from collections %s", affected_ids)

            # Re-dump affected collections
            for cid in affected_ids:
                await dump_collection(cid)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("expiry_cleanup: unexpected error")


async def _purge_loop() -> None:
    """Physically delete items soft-deleted more than DIFF_RETENTION_DAYS ago.

    Diff files for those days are no longer reachable via the public listing
    (window is bounded by today - DIFF_RETENTION_DAYS), so the underlying rows
    can be safely removed.
    """
    settings = get_settings()
    factory = get_session_factory()

    while True:
        await asyncio.sleep(settings.expiry_check_interval)
        try:
            async with factory() as session:
                cutoff = datetime.now(tz=timezone.utc) - timedelta(days=DIFF_RETENTION_DAYS)
                result = await session.execute(
                    delete(Item)
                    .where(Item.deleted_at.isnot(None))
                    .where(Item.deleted_at <= cutoff)
                )
                await session.commit()
                deleted = getattr(result, "rowcount", 0) or 0
                if deleted:
                    log.info("purge_loop: physically removed %d soft-deleted items older than %dd", deleted, DIFF_RETENTION_DAYS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("purge_loop: unexpected error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task, _purge_task

    engine = get_engine()
    factory = get_session_factory()

    # Wait for the database to be ready (retries handle postgres init-script race condition)
    max_retries = 10
    for attempt in range(1, max_retries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            log.info("startup: database connection established")
            break
        except Exception as exc:
            if attempt == max_retries:
                log.error("startup: database unreachable after %d attempts — aborting", max_retries)
                raise
            wait = min(2 ** attempt, 30)
            log.warning("startup: database not ready (attempt %d/%d): %s — retrying in %ss", attempt, max_retries, exc, wait)
            await asyncio.sleep(wait)

    # Create tables + seed under a Postgres advisory lock so that concurrent
    # gunicorn workers don't race on CREATE TABLE / seed INSERTs (which would
    # otherwise surface as UniqueViolationError on pg_type_typname_nsp_index
    # or on seed uniqueness constraints).
    # Lock key is an arbitrary fixed int64 scoped to this service.
    _INIT_LOCK_KEY = 0x6375723031  # 'cur01'
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _INIT_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column upgrades for already-existing deployments.
        # create_all() does not add columns to existing tables.
        await conn.execute(text(
            "ALTER TABLE curator_items "
            "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE curator_items "
            "ADD COLUMN IF NOT EXISTS re_added_at TIMESTAMPTZ NULL"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_curator_items_deleted_at "
            "ON curator_items (deleted_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_curator_items_re_added_at "
            "ON curator_items (re_added_at)"
        ))
        await conn.execute(text(
            "ALTER TABLE curator_collections "
            "ADD COLUMN IF NOT EXISTS published BOOLEAN NOT NULL DEFAULT TRUE"
        ))
    log.info("startup: database tables created (if not exist)")

    # Seed default collections
    async with factory() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _INIT_LOCK_KEY})
        await run_seed(session)
    log.info("startup: seed completed")

    # Start expiry cleanup background task
    _cleanup_task = asyncio.create_task(_expiry_cleanup_loop(), name="expiry-cleanup")
    log.info("startup: expiry cleanup task started (interval=%ss)", get_settings().expiry_check_interval)

    # Start physical purge task for soft-deleted rows past retention
    _purge_task = asyncio.create_task(_purge_loop(), name="diff-purge")
    log.info("startup: purge task started (retention=%dd)", DIFF_RETENTION_DAYS)

    yield

    # Shutdown
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass

    if _purge_task:
        _purge_task.cancel()
        try:
            await _purge_task
        except asyncio.CancelledError:
            pass

    await engine.dispose()
    log.info("shutdown: complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Curator",
        version="1.0.0",
        description="Reputation list management service",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.include_router(admin_router)
    app.include_router(data_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
