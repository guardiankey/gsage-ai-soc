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
    from src.backend_api.app.services.agent_continuation import (
        ContinuationSkipped,
        _is_transient_continuation_error,
    )

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
        # Only retry on transient errors. Non-transient errors should not
        # consume retries — the user already saw the error in chat (Phase 1).
        if not _is_transient_continuation_error(str(exc)):
            log.warning(
                "continue_after_bg_task_completed: non-transient error, "
                "skipping retry task_id=%s",
                task_id,
            )
            _post_continuation_error_message(task_id=task_id, error=str(exc))
            return
        # Transient: retry; on final failure, post error message.
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            log.error(
                "continue_after_bg_task_completed: retries exhausted task_id=%s",
                task_id,
            )
            _post_continuation_error_message(task_id=task_id, error=str(exc))


@celery_app.task(
    bind=True,
    acks_late=True,
    max_retries=2,
    default_retry_delay=30,
    name="src.backend_api.app.tasks.agent_continuation.continue_after_approval_resolved",
)
def continue_after_approval_resolved(self, approval_id: str, org_id: str) -> None:  # type: ignore[misc]
    """Resume the paused agent run after approval and deliver the result."""
    from src.backend_api.app.services.agent_continuation import (
        ContinuationSkipped,
        _is_transient_continuation_error,
    )

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
        if not _is_transient_continuation_error(str(exc)):
            log.warning(
                "continue_after_approval_resolved: non-transient error, "
                "skipping retry approval_id=%s",
                approval_id,
            )
            _post_continuation_error_message(approval_id=approval_id, org_id=org_id, error=str(exc))
            return
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            log.error(
                "continue_after_approval_resolved: retries exhausted approval_id=%s",
                approval_id,
            )
            _post_continuation_error_message(approval_id=approval_id, org_id=org_id, error=str(exc))


def _post_continuation_error_message(
    *,
    task_id: str | None = None,
    approval_id: str | None = None,
    org_id: str | None = None,
    error: str = "",
) -> None:
    """Best-effort delivery of a friendly error message to the user.

    Called after retries are exhausted (or a non-transient error occurs)
    so the user is not left without feedback. Never raises.
    """
    try:
        asyncio.run(_async_post_continuation_error(
            task_id=task_id,
            approval_id=approval_id,
            org_id=org_id,
            error=error,
        ))
    except Exception as exc:
        log.error(
            "_post_continuation_error_message failed: %s",
            exc, exc_info=True,
        )


async def _async_post_continuation_error(
    *,
    task_id: str | None,
    approval_id: str | None,
    org_id: str | None,
    error: str,
) -> None:
    """Resolve the originating session and deliver a friendly error message."""
    import uuid

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.shared.config.settings import get_settings
    from src.shared.database import create_pooled_engine
    from src.shared.models.background_task import GSageBackgroundTask
    from src.shared.models.tenant_session import GSageTenantSession

    settings = get_settings()
    engine = create_pooled_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    friendly = (
        "I could not finish processing your request due to a problem with "
        "the LLM provider. Please try again."
    )
    if error:
        friendly = f"{friendly}\n\n_Details: {error}_"

    try:
        async with session_factory() as db:
            tenant_session: GSageTenantSession | None = None

            if task_id is not None:
                row = await db.execute(
                    select(GSageBackgroundTask).where(
                        GSageBackgroundTask.id == uuid.UUID(task_id)
                    )
                )
                task = row.scalar_one_or_none()
                if task is not None:
                    tenant_session = await db.get(
                        GSageTenantSession, task.gsage_session_id
                    )
            elif approval_id is not None and org_id is not None:
                # Resolve via Agno approval row to find agno_session_id
                from src.backend_api.app.services.agent_factory import get_agno_db

                appr = await get_agno_db().get_approval(approval_id)
                agno_sid = appr.get("session_id") if appr else None
                if agno_sid:
                    row = await db.execute(
                        select(GSageTenantSession).where(
                            GSageTenantSession.agno_session_id == agno_sid,
                            GSageTenantSession.org_id == uuid.UUID(org_id),
                        )
                    )
                    tenant_session = row.scalar_one_or_none()

            if tenant_session is None:
                log.warning(
                    "Could not resolve tenant session for continuation error "
                    "(task_id=%s approval_id=%s)", task_id, approval_id,
                )
                return

            from src.backend_api.app.services.channel_sender import deliver_response

            await deliver_response(tenant_session, friendly, db)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _async_continue_bg_task(task_id: str) -> None:
    """Core async logic for background task continuation."""
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.shared.config.settings import get_settings
    from src.shared.database import create_pooled_engine

    settings = get_settings()
    engine = create_pooled_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            from src.backend_api.app.services.agent_continuation import (
                ContinuationSkipped,
                continue_after_bg_task,
            )
            from src.backend_api.app.services.agno_session_lock import (
                publish_conversation_updated,
            )
            from src.backend_api.app.services.channel_sender import deliver_response

            tenant_session, response_text = await continue_after_bg_task(task_id, db)
            if response_text:
                await deliver_response(tenant_session, response_text, db)

            # Notify SSE subscribers (web clients viewing the conversation) so
            # they refetch immediately instead of waiting for the 5s poll.
            await publish_conversation_updated(
                tenant_session.id, reason="bg_task_completed"
            )

            log.info(
                "continue_after_bg_task_completed: delivered task=%s session=%s source=%s",
                task_id, tenant_session.id, tenant_session.source,
            )
    finally:
        await engine.dispose()


async def _async_continue_approval(approval_id: str, org_id: str) -> None:
    """Core async logic for approval continuation."""
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.shared.config.settings import get_settings
    from src.shared.database import create_pooled_engine

    settings = get_settings()
    engine = create_pooled_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            from src.backend_api.app.services.agent_continuation import continue_after_approval
            from src.backend_api.app.services.agno_session_lock import (
                publish_conversation_updated,
            )
            from src.backend_api.app.services.channel_sender import deliver_response

            tenant_session, response_text = await continue_after_approval(
                approval_id, uuid.UUID(org_id), db
            )
            await deliver_response(tenant_session, response_text, db)

            # Notify SSE subscribers (web clients viewing the conversation) so
            # they refetch immediately instead of waiting for the 5s poll.
            await publish_conversation_updated(
                tenant_session.id, reason="approval_resolved"
            )

            log.info(
                "continue_after_approval_resolved: delivered approval=%s session=%s source=%s",
                approval_id, tenant_session.id, tenant_session.source,
            )
    finally:
        await engine.dispose()
