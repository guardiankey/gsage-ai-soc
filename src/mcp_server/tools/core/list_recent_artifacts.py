"""gSage AI — list_recent_artifacts MCP tool.

Lists the most recent files accessible to the current user/session:

- ``generated``  — files produced by tools (``generate_document``,
  ``zip_tool``, CSV builders, …).
- ``attachment`` — files uploaded by the user as chat attachments.

This is the recovery path when the agent loses the original ``ToolResult``
of a long-running tool call (e.g. ``generate_document`` finishing in the
background after a timeout) and also the canonical way to discover chat
attachments belonging to the current conversation.

For reading the textual content of an attachment, use ``read_file``.
"""

from __future__ import annotations

import time
from typing import ClassVar, Optional

from sqlalchemy import select

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext


class ListRecentArtifactsTool(BaseTool):
    """List files accessible to the current user (tool outputs and chat attachments).

    Use this tool to discover files relevant to the conversation:

    - Files produced by other tools (``generate_document``, ``zip_tool``,
      CSV builders, …) — ``category="generated"``.
    - Files uploaded by the user as chat attachments —
      ``category="attachment"``.

    Filters
    -------
    - ``scope``: ``"all"`` (default) — all files the user can see; or
      ``"session"`` — restrict to the current chat session only. When
      listing attachments, ``"session"`` is usually what you want.
    - ``category``: ``"all"`` (default), ``"generated"``, or
      ``"attachment"``.
    - ``tool_name``: limit to a specific producer tool (e.g.
      ``"generate_document"``). Ignored for attachments.
    - ``limit``: max rows returned (default 20, max 100).
    - ``include_expired``: when False (default), files past their TTL are
      hidden.

    Visibility
    ----------
    Regardless of ``scope``, visibility is always bounded to:

    - Files owned by the current user (any scope).
    - Files explicitly shared with the user's department
      (``scope="department"`` on the file).

    Files belonging to other users / other departments are never visible.
    Admin permissions (``files:read:all``) do **not** bypass this
    restriction for generated files.

    Use the returned ``file_id`` with ``read_file`` (text content) or
    with the ``download_path`` to retrieve the bytes via the API.

    Permission: ``agents:run``
    """

    name: ClassVar[str] = "list_recent_artifacts"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "List files accessible to the current user — both tool-generated "
        "artifacts and chat attachments (own files + department-shared)"
    )
    category: ClassVar[str] = "file"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["agents:run"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 15
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["all", "session"],
                "default": "all",
                "description": (
                    "Listing scope. 'all' (default) returns every file the "
                    "user can see (own files + department-shared files) across "
                    "all conversations. 'session' restricts to files created "
                    "in the current chat session only. Use 'session' to find "
                    "attachments uploaded in the current conversation."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["all", "generated", "attachment"],
                "default": "all",
                "description": (
                    "File category. 'generated' = produced by tools. "
                    "'attachment' = uploaded by the user as a chat attachment. "
                    "'all' (default) returns both."
                ),
            },
            "tool_name": {
                "type": "string",
                "description": (
                    "Optional producer-tool name filter "
                    "(e.g. 'generate_document', 'zip_tool'). "
                    "Only applies to generated files."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum number of artifacts (default 20).",
            },
            "include_expired": {
                "type": "boolean",
                "description": (
                    "Include files past their TTL. Defaults to False."
                ),
            },
        },
        "required": [],
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        from datetime import datetime, timezone  # noqa: PLC0415

        from src.shared.database import _get_session_maker  # noqa: PLC0415
        from src.shared.models.generated_file import GSageFile  # noqa: PLC0415
        from src.mcp_server.tenant_context import (  # noqa: PLC0415
            get_tenant_headers_or_none,
        )

        t0 = time.monotonic()

        listing_scope: str = str(params.get("scope") or "all").strip().lower()
        if listing_scope not in ("all", "session"):
            listing_scope = "all"

        category_filter: str = str(params.get("category") or "all").strip().lower()
        if category_filter not in ("all", "generated", "attachment"):
            category_filter = "all"

        tool_filter: Optional[str] = params.get("tool_name") or None
        limit = min(int(params.get("limit") or 20), 100)
        include_expired = bool(params.get("include_expired") or False)

        tenant = get_tenant_headers_or_none()
        session_id = tenant.gsage_session_id if tenant else None

        async with _get_session_maker()() as db:
            allowed_categories = (
                ["generated", "attachment"]
                if category_filter == "all"
                else [category_filter]
            )
            stmt = (
                select(GSageFile)
                .where(
                    GSageFile.org_id == agent_context.org_id,
                    GSageFile.category.in_(allowed_categories),
                    GSageFile.purged_at.is_(None),
                )
            )

            # Always apply visibility: own files + dept-shared files.
            from sqlalchemy import or_  # noqa: PLC0415

            scope_clauses = [GSageFile.user_id == agent_context.user_id]
            if agent_context.dept_id is not None:
                scope_clauses.append(
                    (GSageFile.scope == "department")
                    & (GSageFile.dept_id == agent_context.dept_id)
                )
            stmt = stmt.where(or_(*scope_clauses))

            # Optionally further narrow to the current session.
            if listing_scope == "session" and session_id:
                stmt = stmt.where(GSageFile.session_id == session_id)

            if tool_filter:
                stmt = stmt.where(GSageFile.tool_name == tool_filter)

            if not include_expired:
                now = datetime.now(timezone.utc)
                stmt = stmt.where(
                    (GSageFile.expires_at.is_(None))
                    | (GSageFile.expires_at > now)
                )

            stmt = stmt.order_by(GSageFile.created_at.desc()).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()

        artifacts = [
            {
                "file_id": str(row.id),
                "filename": row.filename,
                "content_type": row.content_type,
                "size_bytes": row.size_bytes,
                "category": row.category,
                "tool_name": row.tool_name,
                "trace_id": row.trace_id,
                "description": row.description,
                "created_at": (
                    row.created_at.isoformat() if row.created_at else None
                ),
                "expires_at": (
                    row.expires_at.isoformat() if row.expires_at else None
                ),
                "download_path": (
                    f"/v1/orgs/{agent_context.org_id}/files/{row.id}/download"
                ),
            }
            for row in rows
        ]

        return ToolResult.success(
            data={
                "artifacts": artifacts,
                "count": len(artifacts),
                "scope": listing_scope,
                "category": category_filter,
                "session_scoped": listing_scope == "session" and bool(session_id),
                "filtered_by_tool": tool_filter,
            },
            tool_name=self.name,
            version=self.version,
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )
