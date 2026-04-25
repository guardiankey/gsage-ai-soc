"""gSage AI — Organization CRUD tool.

Allows the AI agent to inspect and update the organization settings.

    list    — list organizations visible to the agent         (requires crud:organization:read)
    get     — get details of a specific organization          (requires crud:organization:read)
    update  — update non-sensitive org fields                  (requires crud:organization:write)
    delete  — soft-delete an org (not the current one)        (requires crud:organization:write)

Safety rules:
    - Cannot delete the organization the agent is currently running in.
    - Encrypted fields (llm_api_key) are never exposed.
"""

from __future__ import annotations

import time
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.organization import GSageOrganization
from src.shared.security.context import AgentContext

_PERM_READ = "crud:organization:read"
_PERM_WRITE = "crud:organization:write"

_SAFE_UPDATE_FIELDS = {
    "name", "system_prompt", "default_maker_model", "default_reviewer_model",
    "agent_timeout_seconds", "max_context_tokens", "llm_provider",
}


def _serialize(org: GSageOrganization) -> dict:
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "is_active": org.is_active,
        "system_prompt": org.system_prompt,
        "default_maker_model": org.default_maker_model,
        "default_reviewer_model": org.default_reviewer_model,
        "agent_timeout_seconds": org.agent_timeout_seconds,
        "max_context_tokens": org.max_context_tokens,
        "llm_provider": org.llm_provider,
        "created_at": org.created_at.isoformat(),
    }


class OrganizationCrudTool(CrudBaseTool):
    """CRUD tool for GSageOrganization."""

    name: ClassVar[str] = "organization"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Manage organization settings: LLM provider, API keys, system prompt, and configuration"
    category: ClassVar[str] = "crud"
    available: ClassVar[bool] = False  # temporarily disabled — UX still maturing
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset({"list", "get", "update", "delete"})
    write_actions: ClassVar[frozenset[str]] = frozenset({"update", "delete"})
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
                "enum": ["list", "get", "update", "delete"],
                "description": (
                    "list: list all organizations. "
                    "get: get details of a specific org by id. "
                    "update: modify org settings. "
                    "delete: soft-delete an org (not the current one)."
                ),
            },
            "org_id": {
                "type": "string",
                "description": "[get/update/delete] Target organization UUID.",
            },
            "fields": {
                "type": "object",
                "description": (
                    f"[update] Fields to update. Allowed: {', '.join(sorted(_SAFE_UPDATE_FIELDS))}."
                ),
            },
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
        result = await session.execute(
            select(GSageOrganization).order_by(GSageOrganization.name)
        )
        orgs = result.scalars().all()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"organizations": [_serialize(o) for o in orgs], "count": len(orgs)},
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
        org_id = params.get("org_id") or str(agent_context.org_id)
        result = await session.execute(
            select(GSageOrganization).where(GSageOrganization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if not org:
            return self._failure(code="NOT_FOUND", message=f"Organization '{org_id}' not found.")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(org), execution_time_ms=elapsed)

    async def _handle_update(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        org_id = params.get("org_id") or str(agent_context.org_id)
        fields: dict = params.get("fields", {})

        if not fields:
            return self._failure(code="INVALID_PARAMS", message="'fields' is required for update.")

        invalid = set(fields) - _SAFE_UPDATE_FIELDS
        if invalid:
            return self._failure(
                code="INVALID_PARAMS",
                message=f"Fields not allowed to update: {', '.join(sorted(invalid))}. "
                        f"Allowed: {', '.join(sorted(_SAFE_UPDATE_FIELDS))}.",
            )

        result = await session.execute(
            select(GSageOrganization).where(GSageOrganization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if not org:
            return self._failure(code="NOT_FOUND", message=f"Organization '{org_id}' not found.")

        for key, value in fields.items():
            setattr(org, key, value)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(org), execution_time_ms=elapsed)

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        org_id = params.get("org_id", "")
        if not org_id:
            return self._failure(code="INVALID_PARAMS", message="'org_id' is required.")

        if str(agent_context.org_id) == str(org_id):
            return self._failure(
                code="FORBIDDEN",
                message="Cannot delete the organization the agent is currently running in.",
            )

        result = await session.execute(
            select(GSageOrganization).where(GSageOrganization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if not org:
            return self._failure(code="NOT_FOUND", message=f"Organization '{org_id}' not found.")

        org.is_active = False
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"id": org_id, "deleted": True, "name": org.name},
            execution_time_ms=elapsed,
        )
