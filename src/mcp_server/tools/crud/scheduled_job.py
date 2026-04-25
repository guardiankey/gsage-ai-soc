"""gSage AI — AI Agents CRUD tool.

Allows the AI agent (or an admin) to manage crontab-based AI agents
stored in gsage_scheduled_jobs.

Actions
-------
create  — create a new AI agent and register it in RedBeat (write)
update  — update schedule / config and re-sync RedBeat     (write)
delete  — delete an AI agent and remove from RedBeat       (write)
activate / deactivate — toggle is_active and sync RedBeat (write)
get     — fetch a single AI agent by ID                   (read)
list    — list all AI agents in the current org (paginated) (read)

Permissions
-----------
  crud:scheduled_job:read   — required for get + list
  crud:scheduled_job:write  — required for create / update / delete / activate / deactivate

Feature flags: CRUD_TOOLS_ENABLED + CRUD_TOOLS_ALLOW_WRITE (same as other CRUD tools).
"""

from __future__ import annotations

import time
import uuid as _uuid
from typing import ClassVar, Optional

from sqlalchemy import select

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.scheduled_job import (
    GSageScheduledJob,
    GSageScheduledJobType,
)
from src.shared.security.context import AgentContext


class ScheduledJobCrudTool(CrudBaseTool):
    """CRUD tool for AI agents (gsage_scheduled_jobs) — manages RedBeat entries automatically."""

    name: ClassVar[str] = "scheduled_job"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Manage Celery scheduled jobs: list, create, update, and enable/disable recurring tasks"
    category: ClassVar[str] = "crud"
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset(
        {"create", "update", "delete", "activate", "deactivate", "get", "list"}
    )
    write_actions: ClassVar[frozenset[str]] = frozenset(
        {"create", "update", "delete", "activate", "deactivate"}
    )
    write_permission: ClassVar[str] = "crud:scheduled_job:write"
    permissions: ClassVar[list[str]] = [
        "crud:scheduled_job:read",
        "crud:scheduled_job:write",
    ]

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "delete", "activate", "deactivate", "get", "list"],
                "description": (
                    "create: register a new AI agent. "
                    "update: change schedule or config. "
                    "delete: permanently remove. "
                    "activate/deactivate: toggle is_active. "
                    "get: fetch a job by ID. "
                    "list: list all org jobs."
                ),
            },
            # --- shared ---
            "job_id": {
                "type": "string",
                "description": "UUID of the AI agent (required for update/delete/activate/deactivate/get).",
            },
            # --- create / update ---
            "name": {
                "type": "string",
                "description": "[create/update] Human-readable agent name.",
            },
            "description": {
                "type": "string",
                "description": "[create/update] Optional description.",
            },
            "job_type": {
                "type": "string",
                "enum": ["PROMPT_RUN", "SYSTEM_TASK"],
                "description": "[create] Job type — PROMPT_RUN or SYSTEM_TASK.",
            },
            "cron_expression": {
                "type": "string",
                "description": "[create/update] 5-field standard crontab, e.g. '*/15 * * * *'.",
            },
            "timezone": {
                "type": "string",
                "default": "UTC",
                "description": "[create/update] IANA timezone for cron evaluation.",
            },
            "starts_at": {
                "type": "string",
                "description": "[create/update] ISO-8601 UTC start of window (null = now).",
            },
            "ends_at": {
                "type": "string",
                "description": "[create/update] ISO-8601 UTC end of window (null = indefinite).",
            },
            "max_runs": {
                "type": "integer",
                "description": "[create/update] Auto-deactivate after N runs. Omit for unlimited.",
            },
            # --- PROMPT_RUN ---
            "prompt_content": {
                "type": "string",
                "description": "[PROMPT_RUN] Prompt text to send to the agent.",
            },
            "prompt_conversation_id": {
                "type": "string",
                "description": "[PROMPT_RUN] Target conversation UUID. Omit to create a fresh one per run.",
            },
            "prompt_output_format": {
                "type": "string",
                "enum": ["markdown", "plain"],
                "default": "markdown",
                "description": "[PROMPT_RUN] Output format.",
            },
            # --- SYSTEM_TASK ---
            "task_name": {
                "type": "string",
                "description": "[SYSTEM_TASK] Fully-qualified Celery task name.",
            },
            "task_kwargs": {
                "type": "object",
                "description": "[SYSTEM_TASK] JSON kwargs to pass to the task.",
            },
            # --- list ---
            "limit": {
                "type": "integer",
                "default": 20,
                "description": "[list] Maximum results to return (max 100).",
            },
            "offset": {
                "type": "integer",
                "default": 0,
                "description": "[list] Pagination offset.",
            },
        },
    }

    # ── Handlers ─────────────────────────────────────────────────────────────

    async def _handle_create(self, agent_context: AgentContext, params: dict, config: dict, session, start: float) -> ToolResult:

        from src.shared.services.scheduled_job_service import sync_to_redbeat

        name = (params.get("name") or "").strip()
        if not name:
            return self._failure("INVALID_PARAMS", "Parameter 'name' is required.")

        job_type_raw = (params.get("job_type") or "").upper()
        try:
            job_type = GSageScheduledJobType(job_type_raw)
        except ValueError:
            return self._failure("INVALID_PARAMS", "job_type must be PROMPT_RUN or SYSTEM_TASK.")

        cron = (params.get("cron_expression") or "").strip()
        if not cron:
            return self._failure("INVALID_PARAMS", "Parameter 'cron_expression' is required.")

        # Validate cron before storing
        try:
            from src.shared.services.scheduled_job_service import _parse_cron
            _parse_cron(cron)
        except ValueError as exc:
            return self._failure("INVALID_PARAMS", str(exc))

        if job_type == GSageScheduledJobType.PROMPT_RUN:
            if not (params.get("prompt_content") or "").strip():
                return self._failure("INVALID_PARAMS", "[PROMPT_RUN] 'prompt_content' is required.")
        else:
            if not (params.get("task_name") or "").strip():
                return self._failure("INVALID_PARAMS", "[SYSTEM_TASK] 'task_name' is required.")

        starts_at = _parse_dt(params.get("starts_at"))
        ends_at = _parse_dt(params.get("ends_at"))

        conv_id = params.get("prompt_conversation_id")
        job = GSageScheduledJob(
            org_id=agent_context.org_id,
            user_id=agent_context.user_id,
            name=name,
            description=params.get("description"),
            job_type=job_type.value,
            cron_expression=cron,
            timezone=params.get("timezone") or "UTC",
            starts_at=starts_at,
            ends_at=ends_at,
            is_active=True,
            max_runs=params.get("max_runs"),
            prompt_content=params.get("prompt_content"),
            prompt_conversation_id=_uuid.UUID(conv_id) if conv_id else None,
            prompt_output_format=params.get("prompt_output_format") or "markdown",
            task_name=params.get("task_name"),
            task_kwargs=params.get("task_kwargs"),
        )
        session.add(job)
        await session.flush()  # obtain job.id

        try:
            key = sync_to_redbeat(job)
            job.redbeat_key = key
        except Exception as exc:
            return self._failure("REDBEAT_ERROR", f"Failed to register with RedBeat: {exc}")

        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(job), execution_time_ms=elapsed)

    async def _handle_update(self, agent_context: AgentContext, params: dict, config: dict, session, start: float) -> ToolResult:
        from src.shared.services.scheduled_job_service import sync_to_redbeat

        job = await _load_job(session, params, agent_context)
        if job is None:
            return self._failure("NOT_FOUND", "AI agent not found.")

        if "name" in params and params["name"]:
            job.name = params["name"].strip()
        if "description" in params:
            job.description = params.get("description")
        if "cron_expression" in params and params["cron_expression"]:
            cron = params["cron_expression"].strip()
            try:
                from src.shared.services.scheduled_job_service import _parse_cron
                _parse_cron(cron)
                job.cron_expression = cron
            except ValueError as exc:
                return self._failure("INVALID_PARAMS", str(exc))
        if "timezone" in params and params["timezone"]:
            job.timezone = params["timezone"]
        if "starts_at" in params:
            job.starts_at = _parse_dt(params.get("starts_at"))
        if "ends_at" in params:
            job.ends_at = _parse_dt(params.get("ends_at"))
        if "max_runs" in params:
            job.max_runs = params.get("max_runs")
        if "prompt_content" in params:
            job.prompt_content = params.get("prompt_content")
        if "prompt_output_format" in params and params["prompt_output_format"]:
            job.prompt_output_format = params["prompt_output_format"]
        if "task_kwargs" in params:
            job.task_kwargs = params.get("task_kwargs")

        try:
            sync_to_redbeat(job)
        except Exception as exc:
            return self._failure("REDBEAT_ERROR", f"Failed to sync with RedBeat: {exc}")

        await session.commit()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(job), execution_time_ms=elapsed)

    async def _handle_delete(self, agent_context: AgentContext, params: dict, config: dict, session, start: float) -> ToolResult:
        from src.shared.services.scheduled_job_service import remove_from_redbeat

        job = await _load_job(session, params, agent_context)
        if job is None:
            return self._failure("NOT_FOUND", "AI agent not found.")

        try:
            remove_from_redbeat(job)
        except Exception as exc:
            return self._failure("REDBEAT_ERROR", f"Failed to remove from RedBeat: {exc}")

        await session.delete(job)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data={"deleted": True, "job_id": str(job.id)}, execution_time_ms=elapsed)

    async def _handle_activate(self, agent_context: AgentContext, params: dict, config: dict, session, start: float) -> ToolResult:
        from src.shared.services.scheduled_job_service import sync_to_redbeat

        job = await _load_job(session, params, agent_context)
        if job is None:
            return self._failure("NOT_FOUND", "AI agent not found.")

        job.is_active = True
        try:
            sync_to_redbeat(job)
        except Exception as exc:
            return self._failure("REDBEAT_ERROR", f"Failed to re-register with RedBeat: {exc}")

        await session.commit()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data={"is_active": True, "job_id": str(job.id)}, execution_time_ms=elapsed)

    async def _handle_deactivate(self, agent_context: AgentContext, params: dict, config: dict, session, start: float) -> ToolResult:
        from src.shared.services.scheduled_job_service import remove_from_redbeat

        job = await _load_job(session, params, agent_context)
        if job is None:
            return self._failure("NOT_FOUND", "AI agent not found.")

        job.is_active = False
        try:
            remove_from_redbeat(job)
        except Exception as exc:
            return self._failure("REDBEAT_ERROR", f"Failed to remove from RedBeat: {exc}")

        await session.commit()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data={"is_active": False, "job_id": str(job.id)}, execution_time_ms=elapsed)

    async def _handle_get(self, agent_context: AgentContext, params: dict, config: dict, session, start: float) -> ToolResult:
        job = await _load_job(session, params, agent_context)
        if job is None:
            return self._failure("NOT_FOUND", "AI agent not found.")

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(job), execution_time_ms=elapsed)

    async def _handle_list(self, agent_context: AgentContext, params: dict, config: dict, session, start: float) -> ToolResult:
        limit = min(int(params.get("limit", 20)), 100)
        offset = max(int(params.get("offset", 0)), 0)

        result = await session.execute(
            select(GSageScheduledJob)
            .where(GSageScheduledJob.org_id == agent_context.org_id)
            .order_by(GSageScheduledJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        jobs = result.scalars().all()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"jobs": [_serialize(j) for j in jobs], "count": len(jobs), "offset": offset, "limit": limit},
            execution_time_ms=elapsed,
        )


# ── Helpers ───────────────────────────────────────────────────────────────


async def _load_job(session, params: dict, agent_context: AgentContext) -> Optional[GSageScheduledJob]:
    job_id_raw = (params.get("job_id") or "").strip()
    if not job_id_raw:
        return None
    try:
        job_id = _uuid.UUID(job_id_raw)
    except ValueError:
        return None

    result = await session.execute(
        select(GSageScheduledJob).where(
            GSageScheduledJob.id == job_id,
            GSageScheduledJob.org_id == agent_context.org_id,
        )
    )
    return result.scalar_one_or_none()


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _serialize(job: GSageScheduledJob) -> dict:
    return {
        "id": str(job.id),
        "org_id": str(job.org_id),
        "user_id": str(job.user_id),
        "name": job.name,
        "description": job.description,
        "job_type": job.job_type,
        "cron_expression": job.cron_expression,
        "timezone": job.timezone,
        "starts_at": job.starts_at.isoformat() if job.starts_at else None,
        "ends_at": job.ends_at.isoformat() if job.ends_at else None,
        "is_active": job.is_active,
        "max_runs": job.max_runs,
        "run_count": job.run_count,
        "prompt_content": job.prompt_content,
        "prompt_conversation_id": str(job.prompt_conversation_id) if job.prompt_conversation_id else None,
        "prompt_output_format": job.prompt_output_format,
        "task_name": job.task_name,
        "task_kwargs": job.task_kwargs,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "last_run_status": job.last_run_status,
        "last_run_result": job.last_run_result,
        "redbeat_key": job.redbeat_key,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
