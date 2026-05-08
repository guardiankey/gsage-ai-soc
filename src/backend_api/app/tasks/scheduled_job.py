"""gSage AI — Celery task for PROMPT_RUN scheduled jobs.

This task is dispatched by the standalone scheduler (``src.scheduler.main``)
whenever a ``GSageScheduledJob`` of type ``PROMPT_RUN`` is due.

Task name (registered in celery_app include list)::

    src.backend_api.app.tasks.scheduled_job.run_prompt_job

Queue: ``scheduled``
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from src.backend_api.app.celery_app import celery_app
from src.shared.logging.context import set_trace_context

log = logging.getLogger(__name__)


@celery_app.task(
    name="src.backend_api.app.tasks.scheduled_job.run_prompt_job",
    bind=True,
    queue="scheduled",
    max_retries=2,
    default_retry_delay=60,
)
def run_prompt_job(self, *, job_id: str, org_id: str | None = None, user_id: str | None = None) -> dict:
    """Execute a PROMPT_RUN scheduled job.

    Loads the job row, resolves the agent pipeline via the tenant context,
    and runs the stored ``prompt_content`` through the agent.

    Parameters
    ----------
    job_id:
        UUID string of the ``GSageScheduledJob`` record.
    org_id:
        UUID string of the owning organisation.  Optional — when omitted
        (e.g. when dispatched by RedBeat with only ``job_id`` in kwargs)
        it is resolved from the job row in the database.
    user_id:
        UUID string of the owning user.  Optional — same fallback as org_id.
    """
    import asyncio

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.shared.config.settings import get_settings
    from src.shared.models.scheduled_job import (
        GSageScheduledJob,
        GSageScheduledJobStatus,
    )

    log.info("run_prompt_job: starting job_id=%s", job_id)

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        job = session.get(GSageScheduledJob, job_id)
        if not job:
            log.warning("run_prompt_job: job %s not found", job_id)
            return {"status": "not_found"}

        # Resolve org_id / user_id from the DB row when not supplied via kwargs.
        # RedBeat stores only job_id in the entry kwargs, so this is the normal
        # path for scheduler-triggered executions.
        effective_org_id = org_id or str(job.org_id)
        effective_user_id = user_id or str(job.user_id)

        if not job.is_active:
            log.warning("run_prompt_job: job %s is inactive — skipped", job_id)
            return {"status": "skipped_inactive"}

        if not job.prompt_content:
            log.warning("run_prompt_job: job %s has no prompt_content — skipped", job_id)
            job.last_run_status = GSageScheduledJobStatus.SKIPPED
            session.commit()
            return {"status": "skipped"}

        prompt = job.prompt_content
        job_name: str = job.name  # captured before the session closes

    trace_id = str(uuid.uuid4())
    set_trace_context(trace_id=trace_id, org_id=effective_org_id, user_id=effective_user_id)

    # Run the agent outside the DB session to keep the connection free.
    t0 = time.monotonic()
    try:
        async def _run_agent() -> str:
            from src.backend_api.app.services.agent_factory import build_agent
            from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
            from agno.run import RunStatus

            ctx = TenantContext(
                org_id=uuid.UUID(effective_org_id),
                user_id=uuid.UUID(effective_user_id),
                org_role="member",
                permissions=permissions_for_role("member"),
            )
            session_id = f"sched_{job_id}"
            agent = build_agent(ctx=ctx, agent_id="cybersecurity", session_id=session_id, source="scheduled")
            try:
                from src.shared.services.kb_context import prepend_kb_hints

                effective_prompt = await prepend_kb_hints(
                    prompt,
                    org_id=ctx.org_id,
                    user_id=ctx.user_id,
                    dept_id=getattr(ctx, "dept_id", None),
                )
                run_output = await agent.arun(effective_prompt)

                # Agno swallows provider errors and returns RunOutput with status=RunStatus.error.
                if getattr(run_output, "status", None) == RunStatus.error:
                    raise RuntimeError(
                        "Agent run failed (LLM provider error): "
                        f"{getattr(run_output, 'content', '')}"
                    )

                # HITL: if the run paused for approval, create delegations
                if getattr(run_output, "status", None) == RunStatus.paused:
                    from sqlalchemy.ext.asyncio import AsyncSession as AS, async_sessionmaker, create_async_engine
                    from src.shared.config.settings import get_settings as _gs
                    from src.backend_api.app.services.approval_delegations import (
                        extract_approval_ids_from_run_output,
                        process_approval_delegations,
                    )
                    from src.shared.models.organization import GSageOrganization

                    _settings = _gs()
                    _engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
                    _sf = async_sessionmaker(_engine, expire_on_commit=False)
                    try:
                        async with _sf() as db:
                            approval_ids = extract_approval_ids_from_run_output(run_output)
                            if approval_ids:
                                org_result = await db.get(GSageOrganization, uuid.UUID(effective_org_id))
                                await process_approval_delegations(
                                    approval_ids=approval_ids,
                                    ctx=ctx,
                                    db=db,
                                    org=org_result,
                                    agno_session_id=session_id,
                                    run_id=str(getattr(run_output, "run_id", "") or ""),
                                )
                                await db.commit()
                                log.info(
                                    "run_prompt_job: job %s paused — created %d delegation(s)",
                                    job_id, len(approval_ids),
                                )
                    finally:
                        await _engine.dispose()

                    content = getattr(run_output, "content", None)
                    return str(content) if content else "[paused — awaiting approval]"

                return str(run_output)
            finally:
                # Cleanup MCP sessions to prevent anyio cancel busy-loop (100% CPU).
                try:
                    from src.shared.services.mcp_cleanup import cleanup_agent_mcp

                    await cleanup_agent_mcp(agent)
                except Exception:
                    log.debug("MCP cleanup failed (ignored)", exc_info=True)

        output = asyncio.run(_run_agent())
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = GSageScheduledJobStatus.SUCCESS
        result_payload: dict = {"output": output[:4000]}
        log.info("run_prompt_job: job %s completed in %dms", job_id, duration_ms)

    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        log.exception("run_prompt_job: job %s failed after %dms", job_id, duration_ms)
        status = GSageScheduledJobStatus.FAILURE
        result_payload = {"error": str(exc)[:500]}
        # Persist failure before retrying so the row reflects the attempt.
        with SessionLocal() as session:
            job = session.get(GSageScheduledJob, job_id)
            if job:
                job.last_run_at = datetime.now(timezone.utc)
                job.last_run_status = status
                job.last_run_result = result_payload
                session.commit()
        _write_agent_run_trace(
            trace_id=trace_id,
            job_id=job_id,
            org_id=effective_org_id,
            user_id=effective_user_id,
            status="failure",
            duration_ms=duration_ms,
            error=str(exc)[:500],
        )
        raise self.retry(exc=exc)

    # Persist success: increment run_count, update timestamps, check max_runs.
    with SessionLocal() as session:
        job = session.get(GSageScheduledJob, job_id)
        if job:
            job.last_run_at = datetime.now(timezone.utc)
            job.last_run_status = status
            job.last_run_result = result_payload
            job.run_count = (job.run_count or 0) + 1
            # Auto-deactivate when max_runs limit is reached.
            if job.max_runs is not None and job.run_count >= job.max_runs:
                job.is_active = False
                log.info(
                    "run_prompt_job: job %s reached max_runs=%d — deactivated",
                    job_id, job.max_runs,
                )
            session.commit()

        # Set the conversation title to the job name so it doesn't show as
        # "Untitled" in the UI.  Only update when title is still NULL so that
        # a user-defined rename is not overwritten on subsequent runs.
        from sqlalchemy import select as sa_select  # noqa: PLC0415
        from src.shared.models.tenant_session import GSageTenantSession  # noqa: PLC0415

        ts = session.execute(
            sa_select(GSageTenantSession).where(
                GSageTenantSession.agno_session_id == f"sched_{job_id}"
            )
        ).scalar_one_or_none()
        if ts is not None and ts.title is None:
            ts.title = job_name
            session.commit()
            log.debug("run_prompt_job: set session title=%r for job %s", job_name, job_id)

    # NOTE: success-path ES trace is handled by persist_agno_run_projection
    # (post_hook) which writes to both agno-traces-* and agent-runs-* indices.
    # Only the failure path below needs a manual trace (post_hook doesn't fire
    # when agent.arun() raises).

    return {"status": "ok", "output": output[:500]}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_agent_run_trace(
    *,
    trace_id: str,
    job_id: str,
    org_id: str,
    user_id: str,
    status: str,
    duration_ms: int,
    error: str | None = None,
) -> None:
    """Fire-and-forget trace record to the ``agent-runs`` ES index."""
    from src.shared.elasticsearch.sync_writer import index_trace

    elapsed_seconds = duration_ms / 1000 if duration_ms is not None else None
    doc: dict = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
        "org_id": org_id,
        "user_id": user_id,
        "conversation_id": job_id,
        "agent_type": "scheduled",
        "source": "scheduled",
        "interface": "scheduled",
        "status": status,
        "has_error": status != "completed",
        "total_duration_ms": duration_ms,
        "elapsed_seconds": elapsed_seconds,
        "tools_count": 0,
    }
    if error:
        doc["error_message"] = error

    index_trace("agent-runs", doc)
