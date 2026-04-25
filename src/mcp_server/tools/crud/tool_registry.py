"""gSage AI — Tool Registry CRUD tool.

Allows the AI agent to inspect and manage the global tool registry.

    list    — list all registered tools                        (requires crud:tool:read)
    get     — get details of a specific tool                   (requires crud:tool:read)
    create  — register a new tool entry                        (requires crud:tool:write)
    update  — update tool metadata or status                   (requires crud:tool:write)
    delete  — remove a tool from the registry                  (requires crud:tool:write)

Notes:
    - Tools are global (not org-scoped).
    - The registry is the source of truth for tool discovery.
"""

from __future__ import annotations

import time
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.tool import GSageTool
from src.shared.security.context import AgentContext

_PERM_READ = "crud:tool:read"
_PERM_WRITE = "crud:tool:write"

_SAFE_UPDATE_FIELDS = {
    "display_name", "description", "category",
    "required_permissions", "timeout_seconds",
    "rate_limit_per_minute", "requires_config", "is_active",
}


def _serialize(tool: GSageTool) -> dict:
    return {
        "id": str(tool.id),
        "name": tool.name,
        "version": tool.version,
        "display_name": tool.display_name,
        "description": tool.description,
        "category": tool.category,
        "required_permissions": tool.required_permissions,
        "timeout_seconds": tool.timeout_seconds,
        "rate_limit_per_minute": tool.rate_limit_per_minute,
        "requires_config": tool.requires_config,
        "is_active": tool.is_active,
        "reset_policy": tool.reset_policy,
        "created_at": tool.created_at.isoformat(),
    }


class ToolRegistryCrudTool(CrudBaseTool):
    """CRUD tool for GSageTool (global tool registry)."""

    name: ClassVar[str] = "tool_registry"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Inspect and manage the global tool registry: list tools, view metadata, toggle availability"
    category: ClassVar[str] = "crud"
    available: ClassVar[bool] = False  # temporarily disabled — UX still maturing
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset({"list", "get", "create", "update", "delete"})
    write_actions: ClassVar[frozenset[str]] = frozenset({"create", "update", "delete"})
    write_permission: ClassVar[str] = _PERM_WRITE

    permissions: ClassVar[list[str]] = [_PERM_READ, _PERM_WRITE]

    config_schema: ClassVar[None] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[None] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "create", "update", "delete"],
                "description": (
                    "list: list all tools in the registry. "
                    "get: get full details of a tool by name or id. "
                    "create: register a new tool entry. "
                    "update: update tool metadata or enable/disable it. "
                    "delete: permanently remove a tool from the registry."
                ),
            },
            "tool_id": {"type": "string", "description": "[get/update/delete] Tool UUID."},
            "name": {"type": "string", "description": "[get/create] Tool name (e.g., dns_lookup)."},
            "version": {"type": "string", "description": "[create] Semantic version (e.g., 1.0.0)."},
            "display_name": {"type": "string", "description": "[create/update] Human-readable name."},
            "description": {"type": "string", "description": "[create/update] Description for LLM and UI."},
            "category": {"type": "string", "description": "[create/update] Category (e.g., dns, network, decode)."},
            "required_permissions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "[create/update] Permission tags required to use this tool.",
            },
            "timeout_seconds": {"type": "integer", "description": "[create/update] Execution timeout."},
            "rate_limit_per_minute": {"type": "integer", "description": "[create/update] Rate limit per org."},
            "requires_config": {"type": "boolean", "description": "[create/update] Whether org config is required."},
            "is_active": {"type": "boolean", "description": "[update] Enable or disable the tool globally."},
        },
    }

    # ── Handlers ─────────────────────────────────────────────────────────────

    async def _handle_list(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        # The in-memory registry is the source of truth — the DB table
        # (gsage_tools) is only used for metadata overrides; always
        # reflect what is actually running.
        from src.mcp_server.registry.registry import get_registry
        registry_tools = get_registry().list_all()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"tools": registry_tools, "count": len(registry_tools)},
            execution_time_ms=elapsed,
        )

    async def _handle_get(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        tool_id = params.get("tool_id", "")
        name = params.get("name", "").strip()
        if not tool_id and not name:
            return self._failure(code="INVALID_PARAMS", message="'tool_id' or 'name' is required.")

        stmt = select(GSageTool)
        if tool_id:
            stmt = stmt.where(GSageTool.id == tool_id)
        else:
            stmt = stmt.where(GSageTool.name == name)

        result = await session.execute(stmt)
        tool = result.scalar_one_or_none()
        ref = tool_id or name
        if not tool:
            return self._failure(code="NOT_FOUND", message=f"Tool '{ref}' not found.")

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(tool), execution_time_ms=elapsed)

    async def _handle_create(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        required = {"name", "version", "display_name", "description", "category"}
        missing = required - set(params)
        if missing:
            return self._failure(
                code="INVALID_PARAMS",
                message=f"Required fields missing: {', '.join(sorted(missing))}.",
            )

        tool = GSageTool(
            name=params["name"],
            version=params["version"],
            display_name=params["display_name"],
            description=params["description"],
            category=params["category"],
            required_permissions=params.get("required_permissions", []),
            input_schema=params.get("input_schema", {}),
            output_schema=params.get("output_schema", {}),
            timeout_seconds=int(params.get("timeout_seconds", 10)),
            rate_limit_per_minute=int(params.get("rate_limit_per_minute", 60)),
            requires_config=bool(params.get("requires_config", False)),
        )
        session.add(tool)
        await session.commit()
        await session.refresh(tool)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(tool), execution_time_ms=elapsed)

    async def _handle_update(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        tool_id = params.get("tool_id", "")
        if not tool_id:
            return self._failure(code="INVALID_PARAMS", message="'tool_id' is required.")

        update_fields = {k: v for k, v in params.items() if k in _SAFE_UPDATE_FIELDS}
        if not update_fields:
            return self._failure(
                code="INVALID_PARAMS",
                message=f"No updatable fields provided. Allowed: {', '.join(sorted(_SAFE_UPDATE_FIELDS))}.",
            )

        result = await session.execute(
            select(GSageTool).where(GSageTool.id == tool_id)
        )
        tool = result.scalar_one_or_none()
        if not tool:
            return self._failure(code="NOT_FOUND", message=f"Tool '{tool_id}' not found.")

        for key, value in update_fields.items():
            setattr(tool, key, value)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(tool), execution_time_ms=elapsed)

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        tool_id = params.get("tool_id", "")
        if not tool_id:
            return self._failure(code="INVALID_PARAMS", message="'tool_id' is required.")

        result = await session.execute(
            select(GSageTool).where(GSageTool.id == tool_id)
        )
        tool = result.scalar_one_or_none()
        if not tool:
            return self._failure(code="NOT_FOUND", message=f"Tool '{tool_id}' not found.")

        await session.delete(tool)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"id": tool_id, "name": tool.name, "deleted": True},
            execution_time_ms=elapsed,
        )
