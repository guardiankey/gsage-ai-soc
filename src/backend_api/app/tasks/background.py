"""gSage AI — Background tool execution Celery task.

Tasks
-----
execute_background_tool
    Load a GSageBackgroundTask row, reconstruct the tool and agent context,
    call tool.execute() directly (skipping background redispatch), persist the
    result, and mark the row completed/failed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from src.backend_api.app.celery_app import celery_app

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    acks_late=True,
    max_retries=2,
    default_retry_delay=30,
    name="src.backend_api.app.tasks.background.execute_background_tool",
)
def execute_background_tool(self, task_id: str) -> None:  # type: ignore[misc]
    """Execute a queued background tool task synchronously via asyncio.run."""
    try:
        asyncio.run(_async_execute_background_tool(task_id))
    except Exception as exc:
        log.error("Background task %s failed: %s", task_id, exc, exc_info=True)
        # Best-effort: mark as failed in DB
        try:
            asyncio.run(_mark_failed(task_id, str(exc)))
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Async implementation
# ---------------------------------------------------------------------------


async def _async_execute_background_tool(task_id: str) -> None:
    """Core async logic: load → run → persist result → mark notified=False."""
    import uuid

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.shared.config.settings import get_settings
    from src.shared.elasticsearch.client import ElasticsearchClient
    from src.shared.models.background_task import GSageBackgroundTask, BackgroundTaskStatus
    from src.shared.security.context import AgentContext
    from src.mcp_server.registry.registry import build_registry
    from src.mcp_server.tools.audit import ToolAuditLogger

    import redis.asyncio as redis

    settings = get_settings()

    # Build isolated database session for this task
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    redis_client: redis.Redis = redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    es_client = ElasticsearchClient()

    try:
        async with session_factory() as session:
            # Load task row
            result = await session.execute(
                select(GSageBackgroundTask).where(
                    GSageBackgroundTask.id == uuid.UUID(task_id)
                )
            )
            task = result.scalar_one_or_none()
            if task is None:
                log.error("Background task %s not found in DB", task_id)
                return

            if task.status != BackgroundTaskStatus.QUEUED:
                log.warning(
                    "Background task %s has status %s — skipping (may be duplicate delivery)",
                    task_id, task.status,
                )
                return

            # Mark running
            task.status = BackgroundTaskStatus.RUNNING
            task.started_at = datetime.now(timezone.utc)
            await session.commit()

            # Reconstruct agent context
            agent_context = AgentContext.from_dict(task.agent_context_data)

            # Get the tool from the registry
            registry = build_registry()
            tool = registry.get_tool(task.tool_name)
            if tool is None:
                raise RuntimeError(f"Tool '{task.tool_name}' not found in registry")

            # Load config and state (mirrors run() steps 4 + state load).
            # Resolution chain must match BaseTool.run(): defaults < env < DB.
            config = await tool.load_config(
                agent_context, session, redis_client, profile_id=task.profile_id
            )
            env_defaults = tool._load_env_defaults()
            effective_config = {**tool.config_defaults, **env_defaults, **(config or {})}
            state = await tool.load_state(
                agent_context, session, profile_id=task.profile_id
            )

            # Execute the core tool logic directly — NOT via run() which would
            # re-check should_run_background() and cause an infinite dispatch loop.
            # Inject the fresh session into the ContextVar so that tools that
            # need DB access inside execute() (e.g. _load_file) use this session
            # instead of falling back to the global session maker (which may
            # hold connections from a different event loop in fork workers).
            from src.mcp_server.tools.base import _tool_session_ctx  # noqa: PLC0415
            _ctx_token = _tool_session_ctx.set(session)
            try:
                tool_result = await asyncio.wait_for(
                    tool.execute(agent_context, dict(task.tool_params), effective_config, state),
                    timeout=tool.timeout_seconds * 3,  # generous timeout in worker
                )
            finally:
                _tool_session_ctx.reset(_ctx_token)

            # Persist state changes
            if state != tool.state_defaults:
                await tool.save_state(
                    agent_context, session, state, profile_id=task.profile_id
                )

            # Persist result. Status mirrors the tool's outcome:
            #   - tool_result.status == "error"  -> task FAILED (with error_message)
            #   - anything else (success/partial/background) -> COMPLETED
            task.result = tool_result.to_dict()
            if tool_result.status == "error":
                task.status = BackgroundTaskStatus.FAILED
                err = tool_result.error or {}
                err_code = err.get("code") or "TOOL_ERROR"
                err_msg = err.get("message") or "Tool returned error status"
                task.error_message = f"[{err_code}] {err_msg}"[:2000]
            else:
                task.status = BackgroundTaskStatus.COMPLETED
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()

            # Audit log
            audit = ToolAuditLogger(es_client)
            completed_at = task.completed_at
            started_at = task.started_at
            if completed_at is not None and started_at is not None:
                elapsed = int((completed_at - started_at).total_seconds() * 1000)
            else:
                elapsed = 0
            error_code = tool_result.error.get("code") if tool_result.error else None
            await audit.log_execution(
                agent_context,
                tool.name,
                tool.version,
                dict(task.tool_params),
                tool_result.status,
                elapsed,
                error_code,
                audit_context=task.audit_context_data or None,
                output_data=tool_result.data if tool.audit_output else None,
            )

            log.info(
                "Background task %s completed: tool=%s status=%s org=%s",
                task_id, task.tool_name, tool_result.status, task.org_id,
            )

            # Dispatch agent continuation — re-run the agent with the result
            # so the user receives the output without sending a new message.
            try:
                from src.backend_api.app.tasks.agent_continuation import (
                    continue_after_bg_task_completed,
                )
                continue_after_bg_task_completed.delay(task_id)
                log.info("Background task %s: dispatched continuation task", task_id)
            except Exception as cont_exc:
                log.warning(
                    "Background task %s: failed to dispatch continuation: %s",
                    task_id, cont_exc,
                )

    except Exception as exc:
        log.error("Background task %s error: %s", task_id, exc, exc_info=True)
        async with session_factory() as session:
            await _mark_failed_in_session(session, task_id, str(exc))
        raise
    finally:
        await redis_client.aclose()
        await engine.dispose()


async def _mark_failed(task_id: str, error_message: str) -> None:
    """Open a fresh DB session to mark a task failed (used in except handler)."""
    import uuid

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.shared.config.settings import get_settings
    from src.shared.models.background_task import GSageBackgroundTask, BackgroundTaskStatus

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            await _mark_failed_in_session(session, task_id, error_message)
    finally:
        await engine.dispose()


async def _mark_failed_in_session(session, task_id: str, error_message: str) -> None:
    import uuid

    from sqlalchemy import select

    from src.shared.models.background_task import GSageBackgroundTask, BackgroundTaskStatus

    result = await session.execute(
        select(GSageBackgroundTask).where(
            GSageBackgroundTask.id == uuid.UUID(task_id)
        )
    )
    task = result.scalar_one_or_none()
    if task is not None:
        task.status = BackgroundTaskStatus.FAILED
        task.error_message = error_message[:2000]
        task.completed_at = datetime.now(timezone.utc)
        await session.commit()
