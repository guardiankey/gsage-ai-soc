"""gSage AI — change_file_scope MCP tool.

Allows the **owner** of a tool-generated file to change its visibility
between ``user`` (private — only the owner can read/download) and
``department`` (visible to all members of the owner's department).

Organization-wide scope is intentionally **not exposed**: tool-generated
artifacts must never be broadcast org-wide to prevent cross-department
data leakage. To distribute a document org-wide, upload it as a template
through the templates UI.

This tool is restricted to **the owner** of the file. Admins do not have
override on this operation. Templates (``category="template"``) are not
accepted — manage their visibility through the templates management
endpoints instead.

Required permissions: ``files:write``
"""

from __future__ import annotations

import time
import uuid
from typing import ClassVar

from sqlalchemy import select

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext


class ChangeFileScopeTool(BaseTool):
    """Change the visibility scope of a tool-generated file you own.

    Use this tool when the user asks to **share** a previously generated
    document with their department, or to **make it private** again.

    Allowed transitions:

    - ``user`` → ``department``: file becomes visible to every member of
      the user's current department. Requires the user to belong to a
      department; otherwise the tool returns ``NO_DEPARTMENT``.
    - ``department`` → ``user``: file becomes private to the owner only.

    Restrictions:

    - Only the **owner** of the file can change its scope (no admin
      override).
    - Only **generated** files are accepted; templates are rejected.
    - The ``organization`` scope is not exposed.

    Parameters
    ----------
    file_id (str):
        UUID of the file to update.
    scope (str):
        Target scope. Either ``"user"`` or ``"department"``.

    Permission: ``files:write``
    """

    name: ClassVar[str] = "change_file_scope"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Change a generated file's visibility between private (user) and "
        "department-shared. Only the file owner can perform this change."
    )
    category: ClassVar[str] = "file"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["files:write"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 10
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id", "scope"],
        "properties": {
            "file_id": {
                "type": "string",
                "description": "UUID of the generated file whose scope to update.",
            },
            "scope": {
                "type": "string",
                "enum": ["user", "department"],
                "description": (
                    "Target visibility scope. 'user' makes the file private "
                    "to the owner. 'department' shares it with all members "
                    "of the owner's department. Organization-wide scope is "
                    "not available for tool-generated files."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        from src.shared.database import _get_session_maker  # noqa: PLC0415
        from src.shared.models.generated_file import GSageFile  # noqa: PLC0415

        t0 = time.monotonic()

        # ── Validate params ──────────────────────────────────────────────
        file_id_raw = str(params.get("file_id") or "").strip()
        scope_raw = str(params.get("scope") or "").strip().lower()

        if not file_id_raw:
            return self._failure("INVALID_INPUT", "'file_id' is required.")
        try:
            file_uuid = uuid.UUID(file_id_raw)
        except ValueError:
            return self._failure(
                "INVALID_INPUT",
                f"'file_id' must be a valid UUID. Got: {file_id_raw!r}",
            )
        if scope_raw not in ("user", "department"):
            return self._failure(
                "INVALID_INPUT",
                f"'scope' must be 'user' or 'department'. Got: {scope_raw!r}",
            )

        # ── Look up the file (must belong to caller's org) ───────────────
        async with _get_session_maker()() as db:
            row = (
                await db.execute(
                    select(GSageFile).where(
                        GSageFile.id == file_uuid,
                        GSageFile.org_id == agent_context.org_id,
                    )
                )
            ).scalar_one_or_none()

            elapsed = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731

            if row is None:
                return self._failure(
                    "FILE_NOT_FOUND",
                    f"File '{file_id_raw}' not found.",
                    execution_time_ms=elapsed(),
                )

            # Owner-only check (no admin override).
            if str(row.user_id) != str(agent_context.user_id):
                return self._failure(
                    "FORBIDDEN",
                    "Only the owner of the file can change its scope.",
                    execution_time_ms=elapsed(),
                )

            # Reject templates — managed through dedicated endpoints.
            if row.category == "template":
                return self._failure(
                    "UNSUPPORTED",
                    "change_file_scope does not operate on templates. "
                    "Manage template visibility through the templates UI.",
                    execution_time_ms=elapsed(),
                )

            if row.purged_at is not None:
                return self._failure(
                    "FILE_PURGED",
                    "File has been purged and its scope cannot be changed.",
                    execution_time_ms=elapsed(),
                )

            # ── Apply the change ─────────────────────────────────────────
            if scope_raw == "department":
                if agent_context.dept_id is None:
                    return self._failure(
                        "NO_DEPARTMENT",
                        "Cannot share with department: you do not belong to "
                        "any department. Ask an administrator to assign you "
                        "to one.",
                        execution_time_ms=elapsed(),
                    )
                row.scope = "department"
                row.dept_id = agent_context.dept_id
            else:  # "user"
                row.scope = "user"
                row.dept_id = None

            await db.commit()

            return self._success(
                data={
                    "file_id": str(row.id),
                    "filename": row.filename,
                    "scope": row.scope,
                    "dept_id": str(row.dept_id) if row.dept_id else None,
                },
                execution_time_ms=elapsed(),
            )
