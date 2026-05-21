"""gSage AI — BaseTool abstract class and ToolResult."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch as _fnmatch
from typing import Any, ClassVar, Optional

import redis.asyncio as redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.elasticsearch.client import ElasticsearchClient
from src.shared.models import GSageToolConfig, GSageToolState
from src.shared.security.context import AgentContext
from src.mcp_server.tools.audit import ToolAuditLogger
from src.mcp_server.tools.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# ContextVar that transports the DB session into execute() and helpers such as
# _load_file() without changing the execute() signature.  Set by run() and also
# by background.py when calling execute() directly (Celery workers).
_tool_session_ctx: ContextVar[Optional[AsyncSession]] = ContextVar(
    "tool_session", default=None
)

# Retry config (per PROMPT.md Phase 4)
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = [1.0, 2.0]  # exponential: 1s then 2s

# Redis key patterns — include profile_id for per-profile isolation
TOOL_CONFIG_CACHE_KEY = "toolcfg:{org_id}:{tool_name}:{profile_id}"
TOOL_RATE_LIMIT_KEY = "ratelimit:{org_id}:{tool_name}:{profile_id}"
TOOL_CONFIG_CACHE_TTL = 300  # 5 minutes


def _has_profile_permission(
    user_permissions: list[str],
    tool_permissions: list[str],
    profile_id: str,
) -> bool:
    """Check whether the user may invoke a tool for a specific config profile.

    Resolution order (first match wins):

    1. ``"*"`` in *user_permissions* — global admin wildcard.
    2. Exact 2-segment base tag, e.g. ``"email:send"`` — grants access to
       **all** profiles (backward-compatible default for existing grants).
    3. Explicit all-profiles wildcard, e.g. ``"email:send:*"``.
    4. Profile-specific grant, e.g. ``"email:send:servidor_a"``.
    5. Glob patterns via :func:`fnmatch`, e.g. ``"email:*"`` or
       ``"email:send:prod_*"``.

    Tools declare 2-segment permissions (e.g. ``["email:send"]``).
    3-segment variants are created by the admin only when per-profile
    restriction is needed.  All existing 2-segment grants continue to work.
    """
    if "*" in user_permissions:
        return True

    for tool_perm in tool_permissions:
        for granted in user_permissions:
            # (2) Legacy 2-segment tag → all profiles
            if granted == tool_perm:
                return True
            # (3) Explicit all-profiles wildcard
            if granted == f"{tool_perm}:*":
                return True
            # (4) Profile-specific grant
            if granted == f"{tool_perm}:{profile_id}":
                return True
            # (5) Glob patterns
            if _fnmatch(tool_perm, granted):
                return True
            if _fnmatch(f"{tool_perm}:{profile_id}", granted):
                return True

    return False


@dataclass
class ToolResult:
    """
    Canonical tool output format (per PROMPT.md Phase 4 spec).

    All tools MUST return this format. The LLM (maker) consumes it uniformly.
    """

    status: str  # "success" | "error" | "partial"
    data: Optional[dict]
    error: Optional[dict]  # {"code": ..., "message": ..., "retryable": bool}
    metadata: dict
    # Internal use only — not serialized to LLM. Captured by tools that
    # handle their own exceptions (e.g. WikijsError) and forwarded to audit log.
    traceback_str: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def success(
        cls,
        data: dict,
        tool_name: str,
        version: str,
        execution_time_ms: int,
    ) -> "ToolResult":
        """Build a successful result."""
        return cls(
            status="success",
            data=data,
            error=None,
            metadata={
                "tool": tool_name,
                "version": version,
                "execution_time_ms": execution_time_ms,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        retryable: bool,
        tool_name: str,
        version: str,
        execution_time_ms: int,
    ) -> "ToolResult":
        """Build an error result."""
        return cls(
            status="error",
            data=None,
            error={"code": code, "message": message, "retryable": retryable},
            metadata={
                "tool": tool_name,
                "version": version,
                "execution_time_ms": execution_time_ms,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @classmethod
    def partial(
        cls,
        data: dict,
        code: str,
        message: str,
        retryable: bool,
        tool_name: str,
        version: str,
        execution_time_ms: int,
    ) -> "ToolResult":
        """Build a partial result (some data + error info)."""
        return cls(
            status="partial",
            data=data,
            error={"code": code, "message": message, "retryable": retryable},
            metadata={
                "tool": tool_name,
                "version": version,
                "execution_time_ms": execution_time_ms,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )


class BaseTool(ABC):
    """
    Abstract base class for all gSage MCP tools.

    Subclasses MUST override:
        - ``name`` (ClassVar[str])
        - ``execute(agent_context, params, config, state)``

    The ``run()`` method orchestrates:
        1. Permission validation
        2. Rate limit check (Redis, per org)
        3. Circuit breaker check (skip for local tools)
        4. Config loading (Redis cache → DB → decrypt → merge defaults)
        5. State loading (DB → state_defaults)
        6. Execute with retry (up to 2 retries, 1s + 2s backoff)
        7. Circuit breaker feedback
        8. State persistence
        9. Audit log (Elasticsearch)
    """

    # ── Tool metadata (override in subclasses) ──────────────────────────────
    name: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    permissions: ClassVar[list[str]] = []
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True  # set False for local tools
    available: ClassVar[bool] = True  # set False for stub/unimplemented tools

    # ── Tool discovery metadata ──────────────────────────────────────────────
    # summary: One-line description shown in search results (<120 chars).
    #          Falls back to the first line of the class docstring if empty.
    # category: Logical grouping (dns, network, email, threat_intel, file,
    #           document, itsm, edr, kb, crud, firewall, security, utility).
    # core_tool: When True, always included in list_tools without needing
    #            search_tools for discovery.  Keep to ≤ 10 tools total.
    summary: ClassVar[str] = ""
    category: ClassVar[str] = "general"
    core_tool: ClassVar[bool] = False

    # ── HITL approval (human-in-the-loop) ───────────────────────────────────
    # When True, the agent intercepts this tool and requires human approval
    # before execution.  The MCP server itself does NOT block — it just
    # advertises the flag via tool annotations so the agent layer can act.
    requires_approval: ClassVar[bool] = False

    # ── Background execution ─────────────────────────────────────────────────
    # When always_background=True, every invocation is immediately dispatched
    # to the Celery worker without attempting synchronous execution.
    # When background_threshold_seconds is set, a synchronous timeout triggers
    # a re-dispatch to Celery instead of returning an error.
    # When background_timeout_seconds is set, the Celery worker uses it as the
    # asyncio.wait_for timeout for tool.execute(); otherwise it falls back to
    # ``timeout_seconds * 3``.  Use this for tools that paginate large datasets
    # (e.g. E-goi list enumeration) where the sync-style heuristic is too tight.
    always_background: ClassVar[bool] = False
    background_threshold_seconds: ClassVar[Optional[int]] = None
    background_timeout_seconds: ClassVar[Optional[int]] = None

    # ── Multi-config profiles ────────────────────────────────────────────────
    # When True, multiple config rows (each with a distinct profile_id) can
    # exist for the same (org, tool).  The agent selects the profile via the
    # ``config_profile`` parameter, which is auto-injected into the schema.
    # State, rate-limiting and circuit breaker are also isolated per profile.
    supports_multiple_configs: ClassVar[bool] = False

    # ── Params schema (describes call parameters for the LLM) ───────────────
    params_schema: ClassVar[Optional[dict]] = None

    # ── Audit field mapping (auto-extract target_entities from params) ───────
    # Maps audit_context key → params key for reliable extraction without
    # depending on the LLM.  Only used for data already present in params.
    # Example: {"target_entities": "ip"} will set
    #   audit_context["target_entities"] = [params["ip"]]
    # unless the LLM already supplied audit_context["target_entities"].
    audit_field_mapping: ClassVar[dict] = {}
    # When True, ToolResult.data is included in the audit log (truncated to 4 KB).
    # Default True — opt-out per tool
    audit_output: ClassVar[bool] = True

    @property
    def effective_params_schema(self) -> dict:
        """Return params_schema with framework fields auto-injected.

        Injected fields (only when applicable):

        * ``config_profile`` — when :attr:`supports_multiple_configs` is
          ``True``.  The ``enum`` of available profile IDs is filled in
          dynamically by ``handle_list_tools`` after querying the database,
          so the LLM always sees the correct choices for the current org.
        * ``_approval_summary`` — when :attr:`requires_approval` is ``True``.
        """
        base: dict = dict(
            self.params_schema
            or {"type": "object", "properties": {}, "additionalProperties": True}
        )

        # ── Multi-config: inject config_profile selector ─────────────────
        if self.supports_multiple_configs:
            props = dict(base.get("properties", {}))
            props["config_profile"] = {
                "type": "string",
                "description": (
                    "Configuration profile ID to use. "
                    "Available profiles are listed in the tool description. "
                    "Defaults to 'default' when omitted."
                ),
                # enum is populated dynamically by handle_list_tools
            }
            base["properties"] = props
            # Not required — 'default' is the implicit fallback

        # ── HITL approval: inject _approval_summary ───────────────────────
        if self.requires_approval:
            props = dict(base.get("properties", {}))
            props["_approval_summary"] = {
                "type": "string",
                "description": (
                    "REQUIRED for approval. A concise, human-readable summary of this "
                    "action and the reason it is being performed — in the user's language. "
                    "Example: 'Block IP 1.1.1.1 due to web server attack, ticket #123'."
                ),
            }
            base["properties"] = props
            required: list = list(base.get("required", []))
            if "_approval_summary" not in required:
                required.append("_approval_summary")
            base["required"] = required

        # ── Audit context: inject _audit_context into every tool ──────────
        props = dict(base.get("properties", {}))
        props["_audit_context"] = {
            "type": "object",
            "description": (
                "Optional business context for team-level audit. "
                "Fill in any field that applies to this invocation. "
                "Used only for human review — never for automated decisions."
            ),
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why this action is being performed.",
                },
                "ticket_id": {
                    "type": "string",
                    "description": (
                        "Related ticket, case, or alert reference "
                        "(e.g. 'JIRA-1234', 'ALERT-567', 'INC-89')."
                    ),
                },
                "severity": {
                    "type": "string",
                    "enum": ["info", "low", "medium", "high", "critical"],
                    "description": "Perceived severity of the event being investigated.",
                },
                "notes": {
                    "type": "string",
                    "description": "Any additional context relevant for auditors.",
                },
            },
            "additionalProperties": True,
        }
        base["properties"] = props
        # _audit_context is never required — omitting it is always valid

        return base

    # ── Config (per-org, encrypted, optional) ───────────────────────────────
    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    requires_config: ClassVar[bool] = False

    # Optional shared-config namespace.  When set, ``load_config`` and
    # ``_load_env_defaults`` first read the row / env vars under the
    # *namespace* identifier and then overlay the row / env vars under
    # ``self.name``.  This lets a family of related tools (e.g. all
    # ``trellix_edr_*``) share OAuth credentials and base URL while still
    # allowing per-tool overrides.  Profiles are scoped per (tool_name,
    # profile_id), so namespace and tool both honour the same profile_id
    # selector.  ``None`` (default) → fall back to legacy single-row
    # behaviour keyed by ``self.name``.
    config_namespace: ClassVar[Optional[str]] = None

    # ── State (per-org, plain JSON, optional) ───────────────────────────────
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"  # "daily" | "monthly" | "never"

    # ── ToolResult helpers (auto-fill tool_name + version) ──────────────────

    def _success(self, data: dict, execution_time_ms: int = 0) -> ToolResult:
        """Build a successful ToolResult pre-filled with this tool's metadata."""
        return ToolResult.success(
            data=data,
            tool_name=self.name,
            version=self.version,
            execution_time_ms=execution_time_ms,
        )

    def _failure(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        execution_time_ms: int = 0,
    ) -> ToolResult:
        """Build an error ToolResult pre-filled with this tool's metadata."""
        return ToolResult.failure(
            code=code,
            message=message,
            retryable=retryable,
            tool_name=self.name,
            version=self.version,
            execution_time_ms=execution_time_ms,
        )

    def _partial(
        self,
        data: dict,
        code: str,
        message: str,
        retryable: bool = False,
        execution_time_ms: int = 0,
    ) -> ToolResult:
        """Build a partial ToolResult pre-filled with this tool's metadata."""
        return ToolResult.partial(
            data=data,
            code=code,
            message=message,
            retryable=retryable,
            tool_name=self.name,
            version=self.version,
            execution_time_ms=execution_time_ms,
        )

    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        ...

    async def should_run_background(self, params: dict, config: dict) -> bool:
        """Return True to dispatch this invocation to the Celery background worker.

        Default returns :attr:`always_background`.  Override to implement
        param-based pre-flight estimation (e.g. based on IP range size).
        """
        return self.always_background

    async def _dispatch_background(
        self,
        agent_context: AgentContext,
        params: dict,
        trigger: str,
        profile_id: str,
        session: AsyncSession,
        gsage_session_id: Optional[uuid.UUID] = None,
        audit_context: Optional[dict] = None,
    ) -> ToolResult:
        """Create a GSageBackgroundTask row and dispatch a Celery task.

        Returns a ToolResult with ``status='background'`` so the LLM can
        inform the user that execution is ongoing.
        """
        # Late imports to avoid circular dependency at module load time.
        from src.shared.models.background_task import GSageBackgroundTask  # noqa: PLC0415
        from src.backend_api.app.celery_app import celery_app  # noqa: PLC0415

        task_db = GSageBackgroundTask(
            org_id=agent_context.org_id,
            user_id=agent_context.user_id,
            gsage_session_id=gsage_session_id,
            tool_name=self.name,
            profile_id=profile_id,
            tool_params=dict(params),
            agent_context_data=agent_context.to_dict(),
            audit_context_data=audit_context or None,
            trigger=trigger,
            status="queued",
        )
        session.add(task_db)
        await session.commit()
        await session.refresh(task_db)

        celery_result = celery_app.send_task(
            "src.backend_api.app.tasks.background.execute_background_tool",
            kwargs={"task_id": str(task_db.id)},
            queue="tools",
        )
        task_db.celery_task_id = celery_result.id
        await session.commit()

        logger.info(
            "Background task queued: task_id=%s tool=%s trigger=%s org=%s",
            task_db.id, self.name, trigger, agent_context.org_id,
        )

        return ToolResult(
            status="background",
            data={
                "task_id": str(task_db.id),
                "tool_name": self.name,
                "trigger": trigger,
                "message": (
                    f"Tool '{self.name}' is running in the background. "
                    "You will be notified of the result in this conversation."
                ),
            },
            error=None,
            metadata={
                "tool": self.name,
                "version": self.version,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def run(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        redis_client: redis.Redis,
        es_client: ElasticsearchClient,
        gsage_session_id: Optional[uuid.UUID] = None,
    ) -> ToolResult:
        """
        Orchestration wrapper: permissions → rate limit → circuit breaker →
        config+state → execute (with retry) → save state → audit log.

        Args:
            agent_context: Request context.
            params: Raw input parameters.
            session: Async DB session.
            redis_client: Redis client.
            es_client: Elasticsearch client.

        Returns:
            ToolResult in canonical format.
        """
        start = time.monotonic()
        audit = ToolAuditLogger(es_client)
        circuit = CircuitBreaker(redis_client)

        # ── 0. Extract framework-injected params ─────────────────────────
        if self.supports_multiple_configs:
            raw_profile = params.pop("config_profile", "default")
            profile_id = (
                str(raw_profile).strip() if isinstance(raw_profile, str) else "default"
            ) or "default"
        else:
            profile_id = "default"

        # Extract audit context supplied by the LLM agent (never forwarded
        # to execute() — audit-only field, stripped from the tool call).
        raw_audit_ctx = params.pop("_audit_context", None)
        audit_context: dict = (
            dict(raw_audit_ctx)
            if isinstance(raw_audit_ctx, dict)
            else {}
        )

        # Merge automatic fields from audit_field_mapping (no LLM dependency).
        # These are extracted from the tool's own params, so they are reliable.
        for audit_key, param_key in self.audit_field_mapping.items():
            if param_key in params and audit_key not in audit_context:
                val = params[param_key]
                if audit_key == "target_entities":
                    # Normalise to a list of strings for consistent ES mapping
                    if isinstance(val, list):
                        audit_context[audit_key] = [str(v) for v in val if v]
                    elif val:
                        audit_context[audit_key] = [str(val)]
                else:
                    audit_context[audit_key] = val

        circuit_key = f"{self.name}:{profile_id}"

        # ── 1. Permission check (profile-aware) ──────────────────────────
        # Tools with empty permissions list are accessible to any authenticated user.
        has_wildcard = "*" in agent_context.permissions
        if self.permissions and not has_wildcard and not _has_profile_permission(
            agent_context.permissions, self.permissions, profile_id
        ):
            elapsed = int((time.monotonic() - start) * 1000)
            profile_hint = (
                f" (profile '{profile_id}')" if self.supports_multiple_configs else ""
            )
            result = self._failure(
                code="PERMISSION_DENIED",
                message=f"Required permissions: {', '.join(self.permissions)}{profile_hint}",
                execution_time_ms=elapsed,
            )
            await audit.log_execution(
                agent_context, self.name, self.version,
                params, "permission_denied", elapsed, "PERMISSION_DENIED",
                audit_context=audit_context or None,
            )
            return result

        # ── 2. Rate limit check (per profile) ───────────────────────────
        rate_key = TOOL_RATE_LIMIT_KEY.format(
            org_id=agent_context.org_id,
            tool_name=self.name,
            profile_id=profile_id,
        )
        count = await redis_client.incr(rate_key)
        if count == 1:
            # First request this minute — set expiry
            await redis_client.expire(rate_key, 60)

        if count > self.rate_limit_per_minute:
            elapsed = int((time.monotonic() - start) * 1000)
            result = self._failure(
                code="RATE_LIMIT_EXCEEDED",
                message=f"Rate limit: {self.rate_limit_per_minute} req/min for {self.name}",
                execution_time_ms=elapsed,
            )
            await audit.log_execution(
                agent_context, self.name, self.version,
                params, "error", elapsed, "RATE_LIMIT_EXCEEDED",
                audit_context=audit_context or None,
            )
            return result

        # ── 3. Circuit breaker check (per profile) ───────────────────────
        if self.use_circuit_breaker and not await circuit.is_available(circuit_key):
            elapsed = int((time.monotonic() - start) * 1000)
            profile_tag = f" [{profile_id}]" if profile_id != "default" else ""
            result = self._failure(
                code="CIRCUIT_OPEN",
                message=(
                    f"Tool {self.name}{profile_tag} circuit is OPEN "
                    "(too many failures). Retry in 60s."
                ),
                retryable=True,
                execution_time_ms=elapsed,
            )
            await audit.log_execution(
                agent_context, self.name, self.version,
                params, "error", elapsed, "CIRCUIT_OPEN",
                audit_context=audit_context or None,
            )
            return result

        # ── 4. Load config and state ─────────────────────────────────────
        config = await self.load_config(agent_context, session, redis_client, profile_id=profile_id)

        # Env-var defaults (TOOL_{NAME}__{FIELD}) are resolved before the
        # requires_config guard so that tools configured via environment
        # variables (e.g. TOOL_THREAT_INTEL_LOOKUP__VIRUSTOTAL_API_KEY) are
        # not incorrectly rejected as missing config.
        env_defaults = self._load_env_defaults()
        effective_config = {**self.config_defaults, **env_defaults, **(config or {})}

        # Config is considered "missing" only when the DB row is absent AND
        # neither env-var defaults nor code-level defaults provide any values.
        if config is None and self.requires_config and not effective_config:
            elapsed = int((time.monotonic() - start) * 1000)
            profile_hint = (
                f" (profile '{profile_id}')" if self.supports_multiple_configs else ""
            )
            result = self._failure(
                code="CONFIG_MISSING",
                message=(
                    f"Tool {self.name}{profile_hint} requires per-org config. "
                    "Contact your admin."
                ),
                execution_time_ms=elapsed,
            )
            await audit.log_execution(
                agent_context, self.name, self.version,
                params, "error", elapsed, "CONFIG_MISSING",
                audit_context=audit_context or None,
            )
            return result

        state = await self.load_state(agent_context, session, profile_id=profile_id)

        # ── 4b. Required-params validation (must happen BEFORE BG dispatch) ──
        # Validate required fields from params_schema before calling execute()
        # so tools get a clear MISSING_PARAM error instead of a raw KeyError —
        # and so always_background tools don't enqueue a Celery task that is
        # guaranteed to fail.
        _schema = self.params_schema or {}
        _required_fields: list[str] = _schema.get("required", [])
        _missing = [f for f in _required_fields if f not in params]
        if _missing:
            elapsed = int((time.monotonic() - start) * 1000)
            missing_str = ", ".join(f"'{f}'" for f in _missing)
            result = self._failure(
                code="MISSING_PARAM",
                message=f"Missing required parameter(s): {missing_str}",
                execution_time_ms=elapsed,
            )
            await audit.log_execution(
                agent_context, self.name, self.version,
                params, "error", elapsed, "MISSING_PARAM",
                audit_context=audit_context or None,
            )
            return result

        # ── 4c. Background pre-flight / always_background dispatch ───────────
        if await self.should_run_background(params, effective_config):
            elapsed = int((time.monotonic() - start) * 1000)
            from src.shared.models.background_task import BackgroundTaskTrigger  # noqa: PLC0415
            bg_trigger = (
                BackgroundTaskTrigger.ALWAYS_BACKGROUND
                if self.always_background
                else BackgroundTaskTrigger.PRE_FLIGHT
            )
            bg_result = await self._dispatch_background(
                agent_context=agent_context,
                params=params,
                trigger=bg_trigger,
                profile_id=profile_id,
                session=session,
                gsage_session_id=gsage_session_id,
                audit_context=audit_context or None,
            )
            await audit.log_execution(
                agent_context, self.name, self.version,
                params, "background", elapsed, None,
                audit_context=audit_context or None,
            )
            return bg_result

        # ── 5. Execute with retry ────────────────────────────────────────
        result: ToolResult = self._failure(
            code="TOOL_NOT_EXECUTED",
            message="Tool did not execute",
        )
        error_traceback: Optional[str] = None
        attempt = 0
        max_attempts = 1 + MAX_RETRIES  # first attempt + retries

        _ctx_token = _tool_session_ctx.set(session)
        try:
            while attempt < max_attempts:
                try:
                    result = await asyncio.wait_for(
                        self.execute(agent_context, params, effective_config, state),
                        timeout=self.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    elapsed = int((time.monotonic() - start) * 1000)
                    # Timeout-fallback: re-dispatch to Celery instead of failing
                    if self.background_threshold_seconds is not None:
                        from src.shared.models.background_task import BackgroundTaskTrigger  # noqa: PLC0415
                        bg_result = await self._dispatch_background(
                            agent_context=agent_context,
                            params=params,
                            trigger=BackgroundTaskTrigger.TIMEOUT_FALLBACK,
                            profile_id=profile_id,
                            session=session,
                            gsage_session_id=gsage_session_id,
                            audit_context=audit_context or None,
                        )
                        await audit.log_execution(
                            agent_context, self.name, self.version,
                            params, "background", elapsed, "TOOL_TIMEOUT_FALLBACK",
                            audit_context=audit_context or None,
                        )
                        return bg_result
                    result = self._failure(
                        code="TOOL_TIMEOUT",
                        message=f"Tool {self.name} timed out after {self.timeout_seconds}s",
                        retryable=True,
                        execution_time_ms=elapsed,
                    )
                except Exception as exc:
                    elapsed = int((time.monotonic() - start) * 1000)
                    error_traceback = traceback.format_exc()
                    logger.exception("Tool %s unexpected error (attempt %d): %s", self.name, attempt + 1, exc)
                    result = self._failure(
                        code="TOOL_ERROR",
                        message=str(exc),
                        retryable=True,
                        execution_time_ms=elapsed,
                    )

                # Check if we should retry
                should_retry = (
                    result.status == "error"
                    and result.error is not None
                    and result.error.get("retryable", False)
                    and attempt < max_attempts - 1
                )

                if not should_retry:
                    break

                attempt += 1
                backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "Tool %s retry %d/%d in %.1fs (code=%s)",
                    self.name, attempt, MAX_RETRIES, backoff,
                    result.error.get("code") if result.error else "?",
                )
                await asyncio.sleep(backoff)
        finally:
            _tool_session_ctx.reset(_ctx_token)

        elapsed = int((time.monotonic() - start) * 1000)

        # ── 6. Circuit breaker feedback (per profile) ────────────────────
        if self.use_circuit_breaker:
            if result.status == "error" and result.error is not None and result.error.get("retryable", False):
                await circuit.record_failure(circuit_key)
            elif result.status in ("success", "partial"):
                await circuit.record_success(circuit_key)

        # ── 7. Save state (per profile) ──────────────────────────────────
        if state != self.state_defaults:
            try:
                await self.save_state(agent_context, session, state, profile_id=profile_id)
            except Exception as save_exc:
                # Do NOT let a state-persistence failure suppress the audit log.
                logger.warning(
                    "Tool %s: failed to save state (profile=%s): %s",
                    self.name, profile_id, save_exc,
                )

        # ── 8. Audit log ─────────────────────────────────────────────────
        error_code = result.error.get("code") if result.error else None
        # Prefer traceback captured from unexpected exception; fall back to
        # one captured by the tool itself (e.g. WikijsError handler).
        final_traceback = error_traceback or result.traceback_str
        await audit.log_execution(
            agent_context, self.name, self.version,
            params, result.status, elapsed, error_code, final_traceback,
            audit_context=audit_context or None,
            output_data=result.data if self.audit_output else None,
        )

        return result

    # ── Config management ────────────────────────────────────────────────────

    def _config_lookup_keys(self) -> list[str]:
        """Tool-name keys to read config from, ordered base→override.

        When ``config_namespace`` is set and differs from ``self.name``,
        the namespace row is consulted first and the per-tool row overlays
        on top.  Otherwise only ``self.name`` is used.
        """
        if self.config_namespace and self.config_namespace != self.name:
            return [self.config_namespace, self.name]
        return [self.name]

    async def load_config(
        self,
        agent_context: AgentContext,
        session: AsyncSession,
        redis_client: redis.Redis,
        *,
        profile_id: str = "default",
    ) -> Optional[dict]:
        """
        Load per-org tool config for a specific profile.

        1. Check Redis cache (TTL 5min)
        2. Cache miss → query DB, decrypt, validate, cache.
        3. Returns None if no config exists (tool uses config_defaults).

        When :attr:`config_namespace` is set, rows for both the namespace
        and ``self.name`` are loaded and merged (shallow), with per-tool
        values taking precedence.
        """
        cache_key = TOOL_CONFIG_CACHE_KEY.format(
            org_id=agent_context.org_id,
            tool_name=self.name,
            profile_id=profile_id,
        )

        cached_raw = await redis_client.get(cache_key)
        if cached_raw is not None:
            return json.loads(cached_raw)

        # Cache miss — query DB.  When a namespace is declared we fetch
        # both rows in one query and merge in lookup order.
        lookup_keys = self._config_lookup_keys()
        stmt = select(GSageToolConfig).where(
            GSageToolConfig.org_id == agent_context.org_id,
            GSageToolConfig.tool_name.in_(lookup_keys),
            GSageToolConfig.profile_id == profile_id,
        )
        result = await session.execute(stmt)
        rows = {row.tool_name: row for row in result.scalars().all()}

        if not rows:
            return None  # Caller uses config_defaults

        # Merge in lookup order (base first, override last).  ``row.config``
        # decrypts on access.
        merged: dict = {}
        for key in lookup_keys:
            row = rows.get(key)
            if row is None:
                continue
            row_config = row.config
            if isinstance(row_config, dict):
                merged.update(row_config)

        if not merged:
            return None

        # Validate against schema (basic required fields check)
        if self.config_schema:
            required = self.config_schema.get("required", [])
            missing = [f for f in required if f not in merged]
            if missing:
                logger.error(
                    "Tool config for %s/%s (profile=%s) missing required fields: %s",
                    self.name, agent_context.org_id, profile_id, missing,
                )
                return None

        # Cache the decrypted, merged config
        await redis_client.setex(cache_key, TOOL_CONFIG_CACHE_TTL, json.dumps(merged))

        return merged

    # ── Environment defaults ─────────────────────────────────────────────────

    def _load_env_defaults(self) -> dict:
        """Load tool config defaults from environment variables.

        Convention: ``TOOL_{TOOL_NAME}__{FIELD_NAME}``
        Example: ``TOOL_PORT_CHECK__CONNECT_TIMEOUT_MS=3000``

        When :attr:`config_namespace` is set, env vars under the namespace
        prefix are read first and per-tool env vars overlay on top, e.g.::

            TOOL_TRELLIX_EDR__CLIENT_ID=...           # shared across family
            TOOL_TRELLIX_EDR_SEARCH_FILES__MAX_ROWS=50  # tool-specific override

        These values sit between ``config_defaults`` (code) and any per-org
        DB row in the resolution chain::

            DB (per-org, encrypted)  >  env (TOOL_*)  >  config_defaults

        Cached on the instance after first call (env vars are static at runtime).
        Sensitive fields (marked ``"sensitive": True`` in config_schema) are
        supported but note they are NOT encrypted — use DB config for secrets
        when per-org isolation is required.
        """
        if not hasattr(self, "_env_defaults_cache"):
            result: dict = {}
            for lookup_name in self._config_lookup_keys():
                prefix = f"TOOL_{lookup_name.upper()}__"
                for key, raw_value in os.environ.items():
                    if not key.startswith(prefix):
                        continue
                    field = key[len(prefix):].lower()
                    result[field] = self._coerce_env_value(field, raw_value)
            self._env_defaults_cache = result
        return self._env_defaults_cache

    def _coerce_env_value(self, field: str, raw: str) -> Any:
        """Coerce a raw env string to the expected type.

        Resolution order:
        1. ``config_schema`` (both flat-dict and JSON-Schema ``properties`` formats).
        2. Type of the matching ``config_defaults`` value.
        3. Keep as string.
        """
        # ── 1. Try config_schema ─────────────────────────────────────────
        schema = self.config_schema or {}
        if "properties" in schema:
            field_info = schema["properties"].get(field, {})
        else:
            field_info = schema.get(field, {})
        field_type = field_info.get("type") if isinstance(field_info, dict) else None

        # ── 2. Fall back to config_defaults value type ───────────────────
        if field_type is None and field in self.config_defaults:
            default_val = self.config_defaults[field]
            if isinstance(default_val, bool):
                field_type = "boolean"
            elif isinstance(default_val, int):
                field_type = "integer"
            elif isinstance(default_val, float):
                field_type = "number"

        # ── 3. Coerce ────────────────────────────────────────────────────
        if field_type == "boolean":
            return raw.lower() in ("true", "1", "yes")
        if field_type == "integer":
            try:
                return int(raw)
            except ValueError:
                logger.warning("TOOL_%s__%s: expected integer, got %r — using string", self.name.upper(), field.upper(), raw)
                return raw
        if field_type == "number":
            try:
                return float(raw)
            except ValueError:
                logger.warning("TOOL_%s__%s: expected number, got %r — using string", self.name.upper(), field.upper(), raw)
                return raw
        if field_type in ("array", "object"):
            stripped = raw.strip()
            if stripped:
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "TOOL_%s__%s: expected JSON %s, got %r — using raw string",
                        self.name.upper(), field.upper(), field_type, raw,
                    )
            return raw
        return raw

    # ── State management ─────────────────────────────────────────────────────

    async def load_state(
        self,
        agent_context: AgentContext,
        session: AsyncSession,
        *,
        profile_id: str = "default",
    ) -> dict:
        """
        Load per-org tool runtime state from DB (scoped to profile).

        Returns state_defaults if no row exists (lazy creation on first save).
        """
        stmt = select(GSageToolState).where(
            GSageToolState.org_id == agent_context.org_id,
            GSageToolState.tool_name == self.name,
            GSageToolState.profile_id == profile_id,
            GSageToolState.dept_id.is_(None),
        )
        result = await session.execute(stmt)
        row = result.scalars().first()

        if row is None:
            return dict(self.state_defaults)

        return dict(row.state)

    async def save_state(
        self,
        agent_context: AgentContext,
        session: AsyncSession,
        state: dict,
        *,
        profile_id: str = "default",
    ) -> None:
        """
        Persist per-org tool runtime state (UPSERT), scoped to profile.

        Creates row on first call, updates on subsequent calls.
        """
        # Validate against schema (basic check)
        if self.state_schema:
            required = self.state_schema.get("required", [])
            missing = [f for f in required if f not in state]
            if missing:
                logger.warning(
                    "Tool state for %s/%s (profile=%s) missing fields: %s — storing anyway",
                    self.name, agent_context.org_id, profile_id, missing,
                )

        existing = await session.execute(
            select(GSageToolState).where(
                GSageToolState.org_id == agent_context.org_id,
                GSageToolState.tool_name == self.name,
                GSageToolState.profile_id == profile_id,
                GSageToolState.dept_id.is_(None),
            )
        )
        row = existing.scalars().first()

        if row is None:
            row = GSageToolState(
                org_id=agent_context.org_id,
                tool_name=self.name,
                profile_id=profile_id,
                state=state,
                reset_policy=self.reset_policy,
            )
            session.add(row)
        else:
            row.state = state
            row.reset_policy = self.reset_policy

        await session.commit()

    async def update_state_atomic(
        self,
        agent_context: AgentContext,
        session: AsyncSession,
        path: str,
        value: Any,
        *,
        profile_id: str = "default",
    ) -> None:
        """
        Atomically update a single JSONB field in tool state (scoped to profile).

        Uses PostgreSQL ``jsonb_set()`` to avoid read-modify-write races.
        Ideal for counter increments under concurrency.

        Args:
            path: JSONB path (e.g., "daily_queries_used")
            value: New value for that path
            profile_id: Config profile this state belongs to.
        """
        json_value = json.dumps(value)
        await session.execute(
            text(
                """
                INSERT INTO gsage_tool_state (id, org_id, tool_name, profile_id, state, reset_policy, updated_at)
                VALUES (:id, :org_id, :tool_name, :profile_id, jsonb_set('{}', :path_arr, :value::jsonb), :policy, now())
                ON CONFLICT (org_id, tool_name, profile_id) WHERE dept_id IS NULL
                DO UPDATE SET
                    state = jsonb_set(gsage_tool_state.state, :path_arr, :value::jsonb),
                    updated_at = now()
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": str(agent_context.org_id),
                "tool_name": self.name,
                "profile_id": profile_id,
                "path_arr": f"{{{path}}}",
                "value": json_value,
                "policy": self.reset_policy,
            },
        )
        await session.commit()

    async def enrich_for_listing(
        self,
        org_id: uuid.UUID,
        session: AsyncSession,
    ) -> Optional[str]:
        """Return an optional description suffix injected into the MCP tool
        description at ``list_tools`` time.

        Called by ``handle_list_tools`` for **every** tool that has an active
        config for the requesting org.  The returned string (if any) is
        appended to the tool's ``__doc__``-derived description so the LLM
        knows exactly which hosts, presets, profiles, etc. are available
        **before** making its first call.

        The default implementation handles :attr:`supports_multiple_configs`
        tools by listing visible profiles — the same behaviour that was
        previously hardcoded in ``handle_list_tools``.

        Override in subclasses to expose configuration-derived context
        (e.g. available SSH hosts, preset commands, credential profiles).

        Returns:
            A plain-text suffix string, or ``None`` if nothing should be added.
        """
        if not self.supports_multiple_configs:
            return None

        profiles = await self.list_config_profiles(org_id, session)
        if not profiles:
            return None

        profile_labels = ", ".join(
            f"{p['profile_id']} ({p['description']})"
            if p.get("description")
            else p["profile_id"]
            for p in profiles
        )
        return f"Profiles: {profile_labels}"

    async def list_config_profiles(
        self,
        org_id: uuid.UUID,
        session: AsyncSession,
    ) -> list[dict]:
        """
        Return all configured profiles for this tool in the given org.

        Each dict has ``profile_id`` and ``description`` (may be None).
        Only meaningful when ``supports_multiple_configs=True``.

        When :attr:`config_namespace` is set, profiles from both the
        namespace and ``self.name`` are returned, deduped by profile_id.
        Per-tool description (when present) takes precedence over the
        namespace description.
        """
        lookup_keys = self._config_lookup_keys()
        stmt = (
            select(
                GSageToolConfig.tool_name,
                GSageToolConfig.profile_id,
                GSageToolConfig.description,
            )
            .where(
                GSageToolConfig.org_id == org_id,
                GSageToolConfig.tool_name.in_(lookup_keys),
            )
            .order_by(GSageToolConfig.profile_id)
        )
        result = await session.execute(stmt)

        # Build dedup map: per-tool row overrides namespace row.  We
        # iterate in lookup order (namespace first, name last) so the
        # later assignment wins.
        priority = {key: idx for idx, key in enumerate(lookup_keys)}
        merged: dict[str, dict] = {}
        rows = list(result.all())
        rows.sort(key=lambda r: priority.get(r.tool_name, 0))
        for row in rows:
            existing = merged.get(row.profile_id)
            description = row.description or (existing or {}).get("description")
            merged[row.profile_id] = {
                "profile_id": row.profile_id,
                "description": description,
            }
        return sorted(merged.values(), key=lambda p: p["profile_id"])

    # ── File helpers ─────────────────────────────────────────────────────

    async def _store_file(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        agent_context: "AgentContext",
        session: AsyncSession,
        description: Optional[str] = None,
        trace_id: Optional[str] = None,
        ttl_hours: Optional[int] = None,
        scope: str = "user",
    ) -> Optional[dict]:
        """Upload *data* to MinIO and record it in the DB.

        Intended for use inside :meth:`execute` implementations.  The helper
        deals with all storage concerns; the subclass only needs to return the
        file dict in its ``ToolResult.data``.

        Parameters
        ----------
        data:
            Raw file bytes.  Must not exceed ``file_max_size_bytes``
            (default 220 MB).
        filename:
            User-facing filename (e.g. "report-2026.csv").
        content_type:
            MIME type (e.g. "text/csv", "application/pdf").
        agent_context:
            Current request context (used for ``org_id`` / ``user_id`` /
            ``dept_id``).
        session:
            Open ``AsyncSession``.  The new file row is added to the
            session but **not committed** — the caller (or BaseTool.run) handles
            the commit lifecycle.
        description:
            Optional human-readable description to store with the file.
        trace_id:
            Optional Agno run/trace ID for correlation.
        ttl_hours:
            Override the global TTL.  Pass ``0`` for no expiry.
        scope:
            Visibility scope. Must be ``"user"`` (private — default) or
            ``"department"`` (visible to all members of the agent's dept).
            ``"organization"`` is **not allowed** for tool-generated files
            to prevent accidental cross-department disclosure.
            When ``"department"`` is requested but the agent has no
            ``dept_id``, the call falls back to ``"user"`` with a warning
            (no error).

        Returns
        -------
        dict or None
            On success: a dict with ``file_id``, ``filename``,
            ``content_type``, ``size_bytes``, ``download_path``, ``expires_at``.
            On failure (MinIO unavailable, size exceeded, …): ``None`` —
            the tool should include a warning in its result instead of raising.

        Note
        ----
        ``download_path`` is an authenticated API path of the form
        ``/v1/orgs/{org_id}/files/{file_id}/download``.  Clients must
        include a valid bearer token when following this path.
        """
        try:
            from src.shared.services.file_store import get_file_store
            from src.mcp_server.tenant_context import get_tenant_headers_or_none

            # ── Resolve scope / dept_id ─────────────────────────────────
            # Reject "organization" — tool-generated files must never be
            # broadcast org-wide. Default to "user" if anything unexpected.
            normalized_scope = (scope or "user").strip().lower()
            if normalized_scope not in ("user", "department"):
                logger.warning(
                    "Tool %s: ignoring unsupported scope %r for generated file; using 'user'.",
                    self.name, scope,
                )
                normalized_scope = "user"

            effective_dept_id: Optional[str] = None
            if normalized_scope == "department":
                if agent_context.dept_id is not None:
                    effective_dept_id = str(agent_context.dept_id)
                else:
                    logger.warning(
                        "Tool %s: scope='department' requested but agent has no dept_id; "
                        "falling back to scope='user'.",
                        self.name,
                    )
                    normalized_scope = "user"

            # Attach the file to the current chat session when available so it
            # can be later discovered via list_recent_artifacts / read_file.
            tenant = get_tenant_headers_or_none()
            session_id = (
                str(tenant.gsage_session_id)
                if tenant and tenant.gsage_session_id
                else None
            )

            store = get_file_store()
            gfile = await store.upload(
                data=data,
                filename=filename,
                content_type=content_type,
                org_id=str(agent_context.org_id),
                user_id=str(agent_context.user_id),
                tool_name=self.name,
                db=session,
                description=description,
                trace_id=trace_id,
                ttl_hours=ttl_hours,
                session_id=session_id,
                scope=normalized_scope,
                dept_id=effective_dept_id,
            )
            await session.commit()
            return {
                "file_id": str(gfile.id),
                "filename": gfile.filename,
                "content_type": gfile.content_type,
                "size_bytes": gfile.size_bytes,
                "download_path": f"/v1/orgs/{agent_context.org_id}/files/{gfile.id}/download",
                "expires_at": gfile.expires_at.isoformat() if gfile.expires_at else None,
                "description": gfile.description,
            }
        except Exception as exc:
            logger.error(
                "Tool %s: failed to store file '%s': %s",
                self.name, filename, exc,
            )
            return None

    async def _replace_file_content(
        self,
        file_id: str,
        data: bytes,
        agent_context: "AgentContext",
        session: AsyncSession,
    ) -> Optional[dict]:
        """Overwrite the bytes of an existing file in MinIO and update its DB record.

        Intended for in-place editing: the ``file_id`` (and all references to
        it) remain valid; only the stored content and ``size_bytes`` change.

        Access control: the file must belong to ``agent_context.org_id``.

        Parameters
        ----------
        file_id:
            UUID of the file to overwrite (``GSageFile.id``).
        data:
            New file bytes.
        agent_context:
            Current request context.  Used to enforce ``org_id`` ownership.
        session:
            Open ``AsyncSession``.  Changes are flushed but **not committed** —
            the caller (or ``BaseTool.run``) handles the commit lifecycle.

        Returns
        -------
        dict or None
            Same shape as :meth:`_store_file` on success.  ``None`` if the
            file is not found, access is denied, or the storage call fails.
        """
        try:
            from src.shared.services.file_store import get_file_store  # noqa: PLC0415
            from src.shared.models.generated_file import GSageFile  # noqa: PLC0415
            from sqlalchemy import select  # noqa: PLC0415

            org_id = str(agent_context.org_id)
            stmt = (
                select(GSageFile)
                .where(
                    GSageFile.id == uuid.UUID(file_id),
                    GSageFile.org_id == uuid.UUID(org_id),
                    GSageFile.purged_at.is_(None),
                )
            )
            result = await session.execute(stmt)
            gfile = result.scalar_one_or_none()
            if gfile is None:
                logger.warning(
                    "Tool %s: _replace_file_content: file %s not found or access denied (org=%s)",
                    self.name, file_id, org_id,
                )
                return None

            store = get_file_store()
            new_size = await store.replace_content(
                storage_key=gfile.storage_key,
                data=data,
                content_type=gfile.content_type,
                category=gfile.category,
            )

            gfile.size_bytes = new_size
            await session.flush()

            return {
                "file_id": str(gfile.id),
                "filename": gfile.filename,
                "content_type": gfile.content_type,
                "size_bytes": new_size,
                "download_path": f"/v1/orgs/{org_id}/files/{gfile.id}/download",
                "expires_at": gfile.expires_at.isoformat() if gfile.expires_at else None,
                "description": gfile.description,
            }
        except Exception as exc:
            logger.error(
                "Tool %s: _replace_file_content failed for file %s: %s",
                self.name, file_id, exc,
            )
            return None

    async def _load_file(
        self,
        file_id: str,
        org_id: str,
        session: Optional[AsyncSession] = None,
        user_id: Optional[str] = None,
        dept_id: Optional[str] = None,
        max_bytes: int = 1 * 1024 * 1024,
    ) -> Optional[dict]:
        """Fetch a file's bytes from MinIO by its DB ID.

        Thin wrapper over :meth:`_load_file_with_reason` that discards the
        failure reason — kept for backward compatibility with callers that
        only need the success payload.

        Returns
        -------
        dict or None
            On success: see :meth:`_load_file_with_reason`.
            On failure (not found, access denied, purged, …): ``None``.
        """
        result, _reason = await self._load_file_with_reason(
            file_id=file_id,
            org_id=org_id,
            session=session,
            user_id=user_id,
            dept_id=dept_id,
            max_bytes=max_bytes,
        )
        return result

    async def _load_file_with_reason(
        self,
        file_id: str,
        org_id: str,
        session: Optional[AsyncSession] = None,
        user_id: Optional[str] = None,
        dept_id: Optional[str] = None,
        max_bytes: int = 1 * 1024 * 1024,
    ) -> tuple[Optional[dict], Optional[str]]:
        """Fetch a file's bytes from MinIO by its DB ID, returning a reason on failure.

        Intended for callers that need to surface the specific failure cause
        (e.g. CSV tools mapping it to an ``error_code``).

        Parameters
        ----------
        file_id:
            UUID string of the ``GSageFile`` record.
        org_id:
            UUID string of the owning organisation (access control).
        session:
            Open ``AsyncSession`` for the DB lookup.
        user_id:
            Optional UUID string of the requesting user.  Required to access
            user-scoped files.
        dept_id:
            Optional UUID string of the requesting user's department.  Required
            to access department-scoped files.
        max_bytes:
            Maximum number of bytes to read from MinIO (default 1 MB).
            Files larger than this cap are truncated; a ``truncated`` flag is
            set in the returned dict.

        Returns
        -------
        tuple[dict | None, str | None]
            On success: ``(payload, None)`` where payload has keys
            ``file_id``, ``filename``, ``content_type``, ``size_bytes``,
            ``data``, ``truncated``.
            On failure: ``(None, reason)`` where ``reason`` is one of
            ``"NOT_FOUND"``, ``"PURGED"``, ``"ACCESS_DENIED"``,
            ``"LOAD_FAILED"``.
        """
        try:
            from sqlalchemy import select as _select
            from src.shared.models.generated_file import GSageFile
            from src.shared.services.file_store import get_file_store
            from src.shared.database import _get_session_maker

            import uuid as _uuid

            async def _do_query(db: AsyncSession) -> Optional[GSageFile]:
                return (
                    await db.execute(
                        _select(GSageFile).where(
                            GSageFile.id == _uuid.UUID(file_id),
                            GSageFile.org_id == _uuid.UUID(org_id),
                        )
                    )
                ).scalar_one_or_none()

            if session is not None:
                row: Optional[GSageFile] = await _do_query(session)
            else:
                # Try the ContextVar-injected session first (set by run() or
                # by background.py when calling execute() directly).  Fall back
                # to the global session maker only when neither is available.
                _ctx_session = _tool_session_ctx.get()
                if _ctx_session is not None:
                    row = await _do_query(_ctx_session)
                else:
                    async with _get_session_maker()() as _session:
                        row = await _do_query(_session)

            if row is None:
                logger.warning("Tool %s: _load_file: file %s not found in org %s", self.name, file_id, org_id)
                return None, "NOT_FOUND"

            if row.purged_at is not None:
                logger.warning("Tool %s: _load_file: file %s has been purged", self.name, file_id)
                return None, "PURGED"

            # Access check: 3-way scope resolution
            # - organization: visible to all members → always allow
            # - department: visible to dept members → allow when dept_id matches
            # - user (private): owner only → allow when user_id matches
            if row.scope == "organization":
                pass  # allow
            elif row.scope == "department":
                if dept_id is None or str(row.dept_id) != dept_id:
                    logger.warning(
                        "Tool %s: _load_file: access denied to dept-scoped file %s (dept_id mismatch)",
                        self.name, file_id,
                    )
                    return None, "ACCESS_DENIED"
            else:
                # scope == "user" or any unknown scope — owner only
                if user_id is None or str(row.user_id) != user_id:
                    logger.warning(
                        "Tool %s: _load_file: access denied to user-scoped file %s "
                        "(owner=%s requester=%s)",
                        self.name, file_id, row.user_id, user_id,
                    )
                    return None, "ACCESS_DENIED"

            store = get_file_store()
            # Read up to max_bytes; truncation is signalled in the return value
            data = await store.get_object_bytes(row.storage_key, category=row.category, max_bytes=max_bytes)
            truncated = row.size_bytes > max_bytes

            return {
                "file_id": str(row.id),
                "filename": row.filename,
                "content_type": row.content_type,
                "size_bytes": row.size_bytes,
                "data": data,
                "truncated": truncated,
            }, None
        except Exception as exc:
            logger.error("Tool %s: failed to load file '%s': %s", self.name, file_id, exc)
            return None, "LOAD_FAILED"
