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
    max_retries=3,
    default_retry_delay=30,
    name="src.backend_api.app.tasks.agent_continuation.continue_after_bg_task_completed",
)
def continue_after_bg_task_completed(self, task_id: str) -> None:  # type: ignore[misc]
    """Re-run the agent after a background tool completes and deliver the result.

    Session-busy deferrals are re-queued with a bounded countdown (15/45/90 s)
    so the continuation runs as soon as the session lock is released (spec B3
    item 2). On final give-up the results stay ``notified=False`` (the next
    user turn injects them) and a conversation update is published.
    """
    from src.backend_api.app.services.agent_continuation import (
        ContinuationSkipped,
        ResultsAlreadyConsumed,
        SessionBusy,
        _is_transient_continuation_error,
    )

    # One countdown per retry attempt (max_retries=3).
    _BUSY_RETRY_COUNTDOWNS = (15, 45, 90)

    try:
        asyncio.run(_async_continue_bg_task(task_id))
    except SessionBusy as exc:
        delay = _BUSY_RETRY_COUNTDOWNS[
            min(self.request.retries, len(_BUSY_RETRY_COUNTDOWNS) - 1)
        ]
        log.info(
            "continue_after_bg_task_completed: session busy task_id=%s "
            "retry=%d/%d countdown=%ds",
            task_id, self.request.retries + 1, self.max_retries, delay,
        )
        try:
            raise self.retry(exc=exc, countdown=delay)
        except self.MaxRetriesExceededError:
            log.error(
                "continue_after_bg_task_completed: retries exhausted on busy "
                "session task_id=%s — results remain pending for next user turn",
                task_id,
            )
            _publish_bg_task_pending(task_id)
    except ResultsAlreadyConsumed as exc:
        log.info(
            "continue_after_bg_task_completed: results already consumed task_id=%s: %s",
            task_id, exc,
        )
        # Logical success — nothing to deliver, no retry.
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


@celery_app.task(
    bind=True,
    acks_late=True,
    max_retries=2,
    default_retry_delay=30,
    name="src.backend_api.app.tasks.agent_continuation.continue_after_interaction_submitted",
)
def continue_after_interaction_submitted(self, interaction_id: str) -> None:  # type: ignore[misc]
    """Re-run the agent after a REPLAN_AGENT interaction is submitted."""
    from src.backend_api.app.services.agent_continuation import (
        _is_transient_continuation_error,
    )

    try:
        asyncio.run(_async_continue_interaction(interaction_id))
    except Exception as exc:
        log.error(
            "continue_after_interaction_submitted failed iid=%s: %s",
            interaction_id, exc, exc_info=True,
        )
        if not _is_transient_continuation_error(str(exc)):
            log.warning(
                "continue_after_interaction_submitted: non-transient error, skipping retry",
            )
            return
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            log.error(
                "continue_after_interaction_submitted: retries exhausted iid=%s",
                interaction_id,
            )


async def _async_continue_interaction(interaction_id: str) -> None:
    """Async wrapper: continue agent after REPLAN_AGENT interaction submission."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.shared.config.settings import get_settings
    from src.shared.database import create_pooled_engine
    from src.backend_api.app.services.agent_continuation import (
        continue_after_interaction,
    )
    from src.backend_api.app.services.agno_session_lock import (
        publish_conversation_updated,
    )
    from src.backend_api.app.services.channel_sender import deliver_response

    settings = get_settings()
    engine = create_pooled_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            tenant_session, response_text = await continue_after_interaction(
                interaction_id, db,
            )
            if response_text:
                await deliver_response(tenant_session, response_text, db)

            # Notify SSE subscribers so the frontend refetches immediately
            await publish_conversation_updated(
                tenant_session.id, reason="interaction_submitted"
            )

            log.info(
                "continue_after_interaction_submitted: delivered iid=%s session=%s",
                interaction_id, tenant_session.id,
            )
    finally:
        await engine.dispose()


def _post_continuation_error_message(
    *,
    task_id: str | None = None,
    approval_id: str | None = None,
    org_id: str | None = None,
    error: str = "",
    friendly: str | None = None,
) -> None:
    """Best-effort delivery of a friendly, sanitized error message to the user.

    Technical details (``error``) are logged only — never shown to the user
    (spec B3 item 3). Called after retries are exhausted (or a non-transient
    error occurs) so the user is not left without feedback. Never raises.
    """
    try:
        asyncio.run(_async_post_continuation_error(
            task_id=task_id,
            approval_id=approval_id,
            org_id=org_id,
            error=error,
            friendly=friendly,
        ))
    except Exception as exc:
        log.error(
            "_post_continuation_error_message failed: %s",
            exc, exc_info=True,
        )


_DEFAULT_CONTINUATION_ERROR_TEXT = (
    "The background task could not be completed automatically."
)


async def _async_post_continuation_error(
    *,
    task_id: str | None,
    approval_id: str | None,
    org_id: str | None,
    error: str,
    friendly: str | None = None,
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

    message = friendly or _DEFAULT_CONTINUATION_ERROR_TEXT

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

            # Technical details stay in the logs — the user only ever sees
            # the sanitized message (spec B3 item 3).
            log.error(
                "continuation error for session=%s (task_id=%s approval_id=%s): %s",
                tenant_session.id, task_id, approval_id, error,
            )

            if (tenant_session.source or "web") == "web":
                await _persist_web_assistant_message(
                    tenant_session.agno_session_id, message,
                )
                from src.backend_api.app.services.agno_session_lock import (
                    publish_conversation_updated,
                )
                await publish_conversation_updated(
                    tenant_session.id, reason="continuation_error"
                )
            else:
                from src.backend_api.app.services.channel_sender import deliver_response

                await deliver_response(tenant_session, message, db)

            # Mark the originating task as notified so the next user turn
            # does not re-inject an already-explained failure.
            if task_id is not None:
                from src.backend_api.app.services.background_tasks import (
                    mark_bg_tasks_notified,
                )
                await mark_bg_tasks_notified([uuid.UUID(task_id)], db)
                try:
                    await db.commit()
                except Exception as exc:
                    log.warning(
                        "_async_post_continuation_error: commit of notified "
                        "flag failed: %s",
                        exc,
                    )
    finally:
        await engine.dispose()


async def _persist_web_assistant_message(agno_session_id: str, text: str) -> None:
    """Persist a standalone assistant message into the Agno session history.

    Used to surface continuation failures to web clients, for which the Agno
    post-hook never runs (no ``agent.run()`` was executed in this path).
    Guarded by the Agno session lock so it never clobbers an in-flight run.
    Best-effort: never raises.
    """
    try:
        import uuid

        from agno.db.base import SessionType
        from agno.models.message import Message
        from agno.run.agent import RunOutput
        from agno.run.base import RunStatus

        from src.backend_api.app.services.agent_factory import get_agno_db
        from src.backend_api.app.services.agno_session_lock import (
            release,
            try_acquire,
        )

        lock_token = await try_acquire(agno_session_id, owner="continuation_error")
        if lock_token is None:
            log.warning(
                "_persist_web_assistant_message: session %s busy — skipping "
                "persistence (results remain pending for next user turn)",
                agno_session_id,
            )
            return

        try:
            agno_db = get_agno_db()
            agno_session = await agno_db.get_session(
                session_id=agno_session_id,
                session_type=SessionType.AGENT,
            )
            if agno_session is None:
                log.warning(
                    "_persist_web_assistant_message: agno session %s not found",
                    agno_session_id,
                )
                return

            message = Message(role="assistant", content=text)
            run = RunOutput(
                run_id=f"continuation-error-{uuid.uuid4()}",
                session_id=agno_session_id,
                status=RunStatus.completed,
                content=text,
                content_type="str",
                messages=[message],
            )
            if agno_session.runs:
                agno_session.runs.append(run)
            else:
                agno_session.runs = [run]
            await agno_db.upsert_session(agno_session)
        finally:
            await release(agno_session_id, lock_token)
    except Exception as exc:
        log.error(
            "_persist_web_assistant_message failed session=%s: %s",
            agno_session_id, exc, exc_info=True,
        )


def _publish_bg_task_pending(task_id: str) -> None:
    """Best-effort publish after a busy-session deferral exhausted its retries.

    Results stay ``notified=False`` and will be injected by the next user
    turn; the publish lets SSE clients re-arm polling in the meantime.
    Never raises.
    """
    try:
        asyncio.run(_async_publish_bg_task_pending(task_id))
    except Exception as exc:
        log.error(
            "_publish_bg_task_pending failed task_id=%s: %s",
            task_id, exc, exc_info=True,
        )


async def _async_publish_bg_task_pending(task_id: str) -> None:
    import uuid

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.backend_api.app.services.agno_session_lock import (
        publish_conversation_updated,
    )
    from src.shared.config.settings import get_settings
    from src.shared.database import create_pooled_engine
    from src.shared.models.background_task import GSageBackgroundTask

    settings = get_settings()
    engine = create_pooled_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            result = await db.execute(
                select(GSageBackgroundTask).where(
                    GSageBackgroundTask.id == uuid.UUID(task_id)
                )
            )
            task = result.scalar_one_or_none()
            if task is None or task.gsage_session_id is None:
                log.warning(
                    "_async_publish_bg_task_pending: task not found or has no "
                    "session: %s",
                    task_id,
                )
                return
            await publish_conversation_updated(
                task.gsage_session_id, reason="bg_task_pending"
            )
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
        # ── Best-effort session management ──────────────────────────
        # The agent run inside continue_after_bg_task may take many
        # seconds (LLM calls, tool executions).  We commit before the
        # long call to release the transaction, and swallow close errors
        # so a dead connection doesn't crash the already-computed result.
        session = session_factory()
        try:
            from src.backend_api.app.services.agent_continuation import (
                ContinuationSkipped,
                continue_after_bg_task,
            )
            from src.backend_api.app.services.agno_session_lock import (
                publish_conversation_updated,
            )
            from src.backend_api.app.services.channel_sender import deliver_response

            tenant_session, response_text = await continue_after_bg_task(task_id, session)
            if response_text:
                await deliver_response(tenant_session, response_text, session)

            await publish_conversation_updated(
                tenant_session.id, reason="bg_task_completed"
            )

            log.info(
                "continue_after_bg_task_completed: delivered task=%s session=%s source=%s",
                task_id, tenant_session.id, tenant_session.source,
            )
        finally:
            try:
                await session.close()
            except Exception:
                pass
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
        session = session_factory()
        try:
            from src.backend_api.app.services.agent_continuation import continue_after_approval
            from src.backend_api.app.services.agno_session_lock import (
                publish_conversation_updated,
            )
            from src.backend_api.app.services.channel_sender import deliver_response

            tenant_session, response_text = await continue_after_approval(
                approval_id, uuid.UUID(org_id), session
            )
            await deliver_response(tenant_session, response_text, session)

            await publish_conversation_updated(
                tenant_session.id, reason="approval_resolved"
            )

            log.info(
                "continue_after_approval_resolved: delivered approval=%s session=%s source=%s",
                approval_id, tenant_session.id, tenant_session.source,
            )
        finally:
            try:
                await session.close()
            except Exception:
                pass
    finally:
        await engine.dispose()
