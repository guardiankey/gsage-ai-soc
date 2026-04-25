"""gSage AI — Celery tasks for agent continuation.

Tasks
-----
continue_after_bg_task_completed
    Dispatched after a background tool finishes with COMPLETED status.
    Re-runs the agent with the results injected and delivers the response
    to the originating channel.

continue_after_approval_resolved
    Dispatched after an approval is resolved (approved).
    Resumes the paused agent run via ``acontinue_run()`` and delivers the
    response to the originating channel.
"""

from __future__ import annotations

import asyncio
import logging

from src.backend_api.app.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    acks_late=True,
    max_retries=2,
    default_retry_delay=30,
    name="src.backend_api.app.tasks.agent_continuation.continue_after_bg_task_completed",
)
def continue_after_bg_task_completed(self, task_id: str) -> None:  # type: ignore[misc]
    """Re-run the agent after a background tool completes and deliver the result."""
    from src.backend_api.app.services.agent_continuation import ContinuationSkipped

    try:
        asyncio.run(_async_continue_bg_task(task_id))
    except ContinuationSkipped as exc:
        log.info(
            "continue_after_bg_task_completed skipped task_id=%s: %s",
            task_id, exc,
        )
        # Not a real error — no retry needed.
    except Exception as exc:
        log.error(
            "continue_after_bg_task_completed failed task_id=%s: %s",
            task_id, exc, exc_info=True,
        )
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    acks_late=True,
    max_retries=2,
    default_retry_delay=30,
    name="src.backend_api.app.tasks.agent_continuation.continue_after_approval_resolved",
)
def continue_after_approval_resolved(self, approval_id: str, org_id: str) -> None:  # type: ignore[misc]
    """Resume the paused agent run after approval and deliver the result."""
    from src.backend_api.app.services.agent_continuation import ContinuationSkipped

    try:
        asyncio.run(_async_continue_approval(approval_id, org_id))
    except ContinuationSkipped as exc:
        log.info(
            "continue_after_approval_resolved skipped approval_id=%s: %s",
            approval_id, exc,
        )
        # Not a real error — no retry needed.
    except Exception as exc:
        log.error(
            "continue_after_approval_resolved failed approval_id=%s: %s",
            approval_id, exc, exc_info=True,
        )
        raise self.retry(exc=exc)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _async_continue_bg_task(task_id: str) -> None:
    """Core async logic for background task continuation."""
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.shared.config.settings import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            from src.backend_api.app.services.agent_continuation import (
                ContinuationSkipped,
                continue_after_bg_task,
            )
            from src.backend_api.app.services.channel_sender import deliver_response

            tenant_session, response_text = await continue_after_bg_task(task_id, db)
            if response_text:
                await deliver_response(tenant_session, response_text, db)

            log.info(
                "continue_after_bg_task_completed: delivered task=%s session=%s source=%s",
                task_id, tenant_session.id, tenant_session.source,
            )
    finally:
        await engine.dispose()


async def _async_continue_approval(approval_id: str, org_id: str) -> None:
    """Core async logic for approval continuation."""
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.shared.config.settings import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            from src.backend_api.app.services.agent_continuation import continue_after_approval
            from src.backend_api.app.services.channel_sender import deliver_response

            tenant_session, response_text = await continue_after_approval(
                approval_id, uuid.UUID(org_id), db
            )
            await deliver_response(tenant_session, response_text, db)

            log.info(
                "continue_after_approval_resolved: delivered approval=%s session=%s source=%s",
                approval_id, tenant_session.id, tenant_session.source,
            )
    finally:
        await engine.dispose()
