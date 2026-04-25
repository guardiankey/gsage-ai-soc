"""gSage AI — CrudBaseTool abstract class.

Intermediate base class for tools that perform CRUD operations directly
on the application database.

Key differences from BaseTool:
- No circuit breaker (local DB — no external dependency to protect)
- Checked against CRUD_TOOLS_ENABLED feature flag at runtime
- Exposes AsyncSession to execute_crud() via contextvars (task-local, concurrency-safe)
- Provides generic helpers for common CRUD patterns (list, delete, access checks)
- Subclasses configure actions via ClassVars and implement _handle_{action} methods
"""

from __future__ import annotations

import contextvars
import time
from typing import Any, Callable, ClassVar, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Task-local storage for the DB session.
# ContextVar is async-task-scoped so concurrent requests never share a session.
_crud_session_var: contextvars.ContextVar[Optional[AsyncSession]] = contextvars.ContextVar(
    "_crud_session_var", default=None
)


class CrudBaseTool(BaseTool):
    """
    Abstract base for CRUD tools that operate directly on the database.

    Subclasses MUST override:
        - ``name``  (ClassVar[str])
        - ``valid_actions``  (ClassVar[frozenset[str]]) — allowed action names
        - ``write_actions``  (ClassVar[frozenset[str]]) — actions requiring write permission
        - ``write_permission`` (ClassVar[str]) — permission tag for write operations

    Subclasses implement ``_handle_{action}()`` methods for each valid action.
    The base class provides standard dispatch and generic helpers:
        - ``_check_write_access()`` — validates write permission + feature flag
        - ``_generic_list()`` — paginated tenant-scoped queries with is_active filter
        - ``_generic_delete()`` — soft-delete by UUID with tenant isolation
    """

    use_circuit_breaker: ClassVar[bool] = False

    # Subclasses MUST set these
    valid_actions: ClassVar[frozenset[str]] = frozenset()
    write_actions: ClassVar[frozenset[str]] = frozenset()
    write_permission: ClassVar[str] = ""

    # ── Standard dispatch logic ──────────────────────────────────────────────

    async def execute_crud(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
        session: AsyncSession,
    ) -> ToolResult:
        """
        Standard dispatcher: validate action → check write access → call handler.
        """
        start = time.monotonic()
        action = params.get("action")

        if not action or action not in self.valid_actions:
            return self._failure(
                code="INVALID_ACTION",
                message=f"Invalid action. Valid actions: {', '.join(sorted(self.valid_actions))}",
            )

        # Check write access for write actions
        if action in self.write_actions:
            error = self._check_write_access(agent_context)
            if error:
                return error

        # Dynamically call _handle_{action}()
        handler = getattr(self, f"_handle_{action}", None)
        if not handler:
            return self._failure(
                code="NOT_IMPLEMENTED",
                message=f"Handler _handle_{action}() not implemented",
            )

        return await handler(agent_context, params, config, session, start)

    # ── Generic helpers ─────────────────────────────────────────────────────

    def _check_write_access(self, agent_context: AgentContext) -> Optional[ToolResult]:
        """
        Check write permission and CRUD_TOOLS_ALLOW_WRITE flag.
        Returns ToolResult error if access denied, None if allowed.
        """
        from src.shared.config.settings import get_settings

        if not get_settings().crud_tools_allow_write:
            return self._failure(
                code="WRITE_DISABLED",
                message="CRUD write operations are disabled. Set CRUD_TOOLS_ALLOW_WRITE=true to enable.",
            )

        if "*" not in agent_context.permissions and self.write_permission not in agent_context.permissions:
            return self._failure(
                code="PERMISSION_DENIED",
                message=f"Missing required permission: {self.write_permission}",
            )

        return None

    async def _generic_list(
        self,
        session: AsyncSession,
        model_class: type,
        org_id: str,
        serializer: Callable[[Any], dict],
        page: int = 1,
        page_size: int = 50,
        include_inactive: bool = False,
    ) -> list[dict]:
        """
        Generic paginated list query with tenant isolation and is_active filter.

        Args:
            session: DB session
            model_class: SQLAlchemy model to query
            org_id: Tenant organization ID
            serializer: Function to convert model instance to dict
            page: 1-based page number
            page_size: Results per page
            include_inactive: Whether to include is_active=False records

        Returns:
            List of serialized model instances
        """
        offset = (page - 1) * page_size
        stmt = (
            select(model_class)
            .where(model_class.organization_id == org_id)
            .offset(offset)
            .limit(page_size)
        )

        if not include_inactive and hasattr(model_class, "is_active"):
            stmt = stmt.where(model_class.is_active)

        result = await session.execute(stmt)
        instances = result.scalars().all()
        return [serializer(inst) for inst in instances]

    async def _generic_delete(
        self,
        session: AsyncSession,
        model_class: type,
        org_id: str,
        uuid: str,
    ) -> ToolResult:
        """
        Generic soft-delete by UUID with tenant isolation.

        Args:
            session: DB session
            model_class: SQLAlchemy model
            org_id: Tenant organization ID
            uuid: Record UUID to delete

        Returns:
            ToolResult with success or error
        """
        stmt = select(model_class).where(
            model_class.organization_id == org_id,
            model_class.uuid == uuid,
        )
        result = await session.execute(stmt)
        instance = result.scalar_one_or_none()

        if not instance:
            return self._failure(
                code="NOT_FOUND",
                message=f"{model_class.__name__} not found with UUID {uuid}",
            )

        instance.is_active = False
        await session.commit()

        return self._success(
            data={
                "uuid": uuid,
                "deleted": True,
                "model": model_class.__name__,
            }
        )

    # ── Shim: satisfy BaseTool.execute() → delegate to execute_crud() ──────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """Retrieve session from context and delegate to execute_crud()."""
        session = _crud_session_var.get()
        if session is None:
            return self._failure(
                code="INTERNAL_ERROR",
                message="DB session not available — CRUD tool invoked outside run() context.",
            )
        return await self.execute_crud(agent_context, params, config, state, session)

    # ── Override run() to set session in context before BaseTool runs ───────

    async def run(
        self,
        agent_context: AgentContext,
        params: dict,
        session: AsyncSession,
        redis_client,
        es_client,
        gsage_session_id=None,
    ) -> ToolResult:
        """
        Check feature flag, store session in ContextVar, then delegate to BaseTool.run().
        """
        from src.shared.config.settings import get_settings

        if not get_settings().crud_tools_enabled:
            return self._failure(
                code="FEATURE_DISABLED",
                message=(
                    "CRUD tools are disabled. "
                    "Set CRUD_TOOLS_ENABLED=true in your environment to enable."
                ),
            )

        token = _crud_session_var.set(session)
        try:
            return await super().run(agent_context, params, session, redis_client, es_client, gsage_session_id=gsage_session_id)
        finally:
            _crud_session_var.reset(token)
