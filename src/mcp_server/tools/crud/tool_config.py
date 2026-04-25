"""gSage AI — Tool Config CRUD tool.

Allows the AI agent to manage per-org tool configurations.

    list    — list all tool configs for current org            (requires crud:tool_config:read)
    get     — get a specific config (sensitive keys redacted)  (requires crud:tool_config:read)
    set     — create or update a tool config                   (requires crud:tool_config:write)
    delete  — delete a tool config                             (requires crud:tool_config:write)

Safety rules:
    - Config values containing secret-like keys are automatically redacted from
      read responses. Secret key patterns: any key whose name contains one of:
      password, secret, key, token, credential, auth, private.
    - The 'set' action accepts the plain config dict; encryption is handled internally.
"""

from __future__ import annotations

import re
import time
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.tool_config import GSageToolConfig
from src.shared.security.context import AgentContext

_PERM_READ = "crud:tool_config:read"
_PERM_WRITE = "crud:tool_config:write"

# Any config key matching this pattern is considered secret and will be redacted.
_SECRET_KEY_RE = re.compile(
    r"(password|secret|key|token|credential|auth|private)", re.IGNORECASE
)


def _redact_config(config: dict) -> dict:
    """Return a copy of config with sensitive values replaced by '***REDACTED***'."""
    return {
        k: "***REDACTED***" if _SECRET_KEY_RE.search(k) else v
        for k, v in config.items()
    }


def _serialize(tc: GSageToolConfig, include_config: bool = False) -> dict:
    data: dict = {
        "id": str(tc.id),
        "org_id": str(tc.org_id),
        "tool_name": tc.tool_name,
        "updated_by_user_id": str(tc.updated_by_user_id) if tc.updated_by_user_id else None,
        "created_at": tc.created_at.isoformat(),
        "updated_at": tc.updated_at.isoformat(),
    }
    if include_config:
        try:
            data["config"] = _redact_config(tc.config)
        except Exception:
            data["config"] = {}
    return data


class ToolConfigCrudTool(CrudBaseTool):
    """CRUD tool for GSageToolConfig (encrypted, sensitive values redacted on read)."""

    name: ClassVar[str] = "tool_config"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Manage per-organization tool configuration profiles (API keys, endpoints, credentials)"
    category: ClassVar[str] = "crud"
    available: ClassVar[bool] = False  # temporarily disabled — UX still maturing
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset({"list", "get", "set", "delete"})
    write_actions: ClassVar[frozenset[str]] = frozenset({"set", "delete"})
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
                "enum": ["list", "get", "set", "delete"],
                "description": (
                    "list: list all tool configs for the org (no config values). "
                    "get: get a config with values (sensitive keys redacted). "
                    "set: create or update a tool config (upsert). "
                    "delete: remove a tool config."
                ),
            },
            "tool_name": {
                "type": "string",
                "description": "[get/set/delete] Tool name (e.g., dns_lookup).",
            },
            "config": {
                "type": "object",
                "description": (
                    "[set] Config dict to store. "
                    "Do NOT include passwords, API keys, tokens, or secrets — "
                    "those must be configured via the administration interface."
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
            select(GSageToolConfig)
            .where(GSageToolConfig.org_id == agent_context.org_id)
            .order_by(GSageToolConfig.tool_name)
        )
        configs = result.scalars().all()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"tool_configs": [_serialize(tc) for tc in configs], "count": len(configs)},
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
        tool_name = params.get("tool_name", "").strip()
        if not tool_name:
            return self._failure(code="INVALID_PARAMS", message="'tool_name' is required.")

        result = await session.execute(
            select(GSageToolConfig).where(
                GSageToolConfig.org_id == agent_context.org_id,
                GSageToolConfig.tool_name == tool_name,
            )
        )
        tc = result.scalar_one_or_none()
        if not tc:
            return self._failure(code="NOT_FOUND", message=f"No config found for tool '{tool_name}'.")

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(tc, include_config=True), execution_time_ms=elapsed)

    async def _handle_set(
        self,
        agent_context: AgentContext,
        params: dict,
        config_param: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        tool_name = params.get("tool_name", "").strip()
        new_config: dict = params.get("config", {})

        if not tool_name:
            return self._failure(code="INVALID_PARAMS", message="'tool_name' is required.")
        if not isinstance(new_config, dict):
            return self._failure(code="INVALID_PARAMS", message="'config' must be an object.")

        # Reject any secret-looking keys up front
        secret_keys = [k for k in new_config if _SECRET_KEY_RE.search(k)]
        if secret_keys:
            return self._failure(
                code="SECURITY_POLICY",
                message=(
                    f"Config keys with sensitive names are not allowed via this tool: "
                    f"{', '.join(secret_keys)}. "
                    "Use the administration interface to set secrets."
                ),
            )

        result = await session.execute(
            select(GSageToolConfig).where(
                GSageToolConfig.org_id == agent_context.org_id,
                GSageToolConfig.tool_name == tool_name,
            )
        )
        tc = result.scalar_one_or_none()

        if tc:
            # Merge: preserve existing secret keys, overwrite non-secret keys
            existing = tc.config
            merged = {
                k: v for k, v in existing.items() if _SECRET_KEY_RE.search(k)
            }
            merged.update(new_config)
            tc.config = merged
            tc.updated_by_user_id = agent_context.user_id
            created = False
        else:
            tc = GSageToolConfig(
                org_id=agent_context.org_id,
                tool_name=tool_name,
                updated_by_user_id=agent_context.user_id,
            )
            tc.config = new_config
            session.add(tc)
            created = True

        await session.commit()
        await session.refresh(tc)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={**_serialize(tc, include_config=True), "created": created},
            execution_time_ms=elapsed,
        )

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        tool_name = params.get("tool_name", "").strip()
        if not tool_name:
            return self._failure(code="INVALID_PARAMS", message="'tool_name' is required.")

        result = await session.execute(
            select(GSageToolConfig).where(
                GSageToolConfig.org_id == agent_context.org_id,
                GSageToolConfig.tool_name == tool_name,
            )
        )
        tc = result.scalar_one_or_none()
        if not tc:
            return self._failure(code="NOT_FOUND", message=f"No config found for tool '{tool_name}'.")

        await session.delete(tc)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"tool_name": tool_name, "org_id": str(agent_context.org_id), "deleted": True},
            execution_time_ms=elapsed,
        )
