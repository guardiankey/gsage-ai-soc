"""gSage AI — ReadFile tool.

Allows the agent to read the contents of a text file attached to
the current chat session, or list all attachments in the conversation.
"""

from __future__ import annotations

import hashlib
import time
from typing import ClassVar

from sqlalchemy import select

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Text content types that can be decoded to UTF-8 and returned as strings.
_TEXT_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/x-sh",
    "application/x-python",
    "application/csv",
)

# Hard cap on bytes read from MinIO (5 MB).
_MAX_READ_BYTES = 5 * 1024 * 1024

# Default maximum lines returned for text content (no filters applied).
_DEFAULT_MAX_LINES = 200


def _is_text(content_type: str) -> bool:
    ct = content_type.lower().split(";")[0].strip()
    return any(ct.startswith(p) for p in _TEXT_MIME_PREFIXES)


class ReadFileTool(BaseTool):
    """
    Read File — read a text file or list conversation attachments.

    **Mode 1 — read** (``file_id`` provided):
    Returns the content of a text-based attachment (plain text, JSON, CSV,
    YAML, script files, etc.) together with file metadata and hashes
    (MD5 / SHA1 / SHA256).

    For **binary files** (images, PDFs, ZIPs, executables, ...) the content
    is never returned — use the tool only to obtain metadata and hashes.
    To read the actual content of a binary file use a dedicated analysis
    tool (e.g. ``pdf_analyzer``, ``eml_analyzer``).

    Text content is always filtered before being returned, so large files
    do not overflow the context:

    - **Line range**: ``start_line`` / ``end_line`` (1-indexed, inclusive).
      Defaults to the first ``max_lines`` lines of the file.
    - **Grep**: ``grep`` matches only lines containing the given substring
      (case-insensitive plain-text match, not regex).
    - **max_lines**: hard cap on the number of lines returned (default 200,
      max 1000).  Applies after line-range and grep filtering.

    When neither ``start_line`` / ``end_line`` nor ``grep`` is provided the
    first 200 lines are returned (or fewer if the file is smaller).
    Call again with different ``start_line`` / ``end_line`` to page through
    a large file.

    **Mode 2 — list** (``file_id`` omitted):
    Returns all attachments for the current conversation session with
    filename, content_type, size_bytes, and ``download_path`` for each.

    Permission: ``agents:run``
    """

    name: ClassVar[str] = "read_file"
    version: ClassVar[str] = "2.0.0"
    summary: ClassVar[str] = "Read a text file attachment from the conversation or list all currently attached files"
    category: ClassVar[str] = "file"
    core_tool: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["agents:run"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the attached file to read. "
                    "Omit to list all attachments in the current conversation."
                ),
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "First line to return (1-indexed, inclusive). "
                    "Defaults to 1. Only applies to text files."
                ),
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Last line to return (1-indexed, inclusive). "
                    "Defaults to start_line + max_lines - 1. Only applies to text files."
                ),
            },
            "grep": {
                "type": "string",
                "description": (
                    "Return only lines containing this substring "
                    "(case-insensitive plain-text match). "
                    "Applied after start_line/end_line filtering."
                ),
            },
            "max_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": (
                    "Maximum number of lines to return (default 200, max 1000). "
                    "Applied last, after start_line/end_line and grep."
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
        file_id: str | None = params.get("file_id") or None
        t0 = time.monotonic()

        if not file_id:
            return await self._list_attachments(agent_context, t0)

        # ── Read mode ────────────────────────────────────────────────────
        result = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_MAX_READ_BYTES,
        )

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if result is None:
            return ToolResult.failure(
                code="FILE_NOT_FOUND",
                message=f"File '{file_id}' not found or access denied.",
                retryable=False,
                tool_name=self.name,
                version=self.version,
                execution_time_ms=elapsed_ms,
            )

        data: bytes = result["data"]
        filename: str = result["filename"]
        content_type: str = result["content_type"]
        size_bytes: int = result["size_bytes"]
        truncated_by_cap: bool = result["truncated"]

        hashes = {
            "md5": hashlib.md5(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        download_path = f"/v1/orgs/{agent_context.org_id}/files/{file_id}/download"

        base_meta: dict = {
            "file_id": file_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "hashes": hashes,
            "download_path": download_path,
        }

        # ── Binary: never return content ──────────────────────────────────
        if not _is_text(content_type):
            return ToolResult.success(
                data={
                    **base_meta,
                    "content_note": (
                        "Binary file — content not returned. "
                        "Use a dedicated analysis tool (pdf_analyzer, eml_analyzer, etc.) "
                        "to inspect the file contents."
                    ),
                },
                tool_name=self.name,
                version=self.version,
                execution_time_ms=elapsed_ms,
            )

        # ── Text: decode ──────────────────────────────────────────────────
        try:
            raw_text = data.decode("utf-8", errors="replace")
        except Exception:
            raw_text = data.decode("latin-1", errors="replace")

        all_lines = raw_text.splitlines()
        total_lines = len(all_lines)
        total_words = len(raw_text.split())
        total_chars = len(raw_text)

        # ── Apply line range ──────────────────────────────────────────────
        raw_start = params.get("start_line")
        raw_end = params.get("end_line")
        max_lines = min(int(params.get("max_lines") or _DEFAULT_MAX_LINES), 1000)

        start_line = max(1, int(raw_start)) if raw_start is not None else 1
        end_line = int(raw_end) if raw_end is not None else start_line + max_lines - 1
        end_line = min(end_line, total_lines)

        selected = all_lines[start_line - 1 : end_line]

        # ── Apply grep filter ─────────────────────────────────────────────
        grep_pattern: str | None = params.get("grep") or None
        grep_applied = False
        if grep_pattern:
            pattern_lower = grep_pattern.lower()
            selected = [ln for ln in selected if pattern_lower in ln.lower()]
            grep_applied = True

        # ── Apply max_lines cap ───────────────────────────────────────────
        capped = len(selected) > max_lines
        selected = selected[:max_lines]

        content_out = "\n".join(selected)

        out_data: dict = {
            **base_meta,
            "encoding": "utf-8",
            "content": content_out,
            "total_lines": total_lines,
            "total_words": total_words,
            "total_chars": total_chars,
            "returned_lines": len(selected),
            "start_line": start_line,
            "end_line": end_line,
            "grep_applied": grep_applied,
            "capped_at_max_lines": capped,
            "file_truncated_by_size": truncated_by_cap,
        }

        if capped or truncated_by_cap:
            return ToolResult.partial(
                data=out_data,
                code="TRUNCATED",
                message=(
                    f"Returned {len(selected)} of {total_lines} lines "
                    f"(start={start_line}, end={end_line}"
                    + (f", grep={grep_pattern!r}" if grep_applied else "")
                    + "). Call again with different start_line/end_line to read more."
                ),
                retryable=False,
                tool_name=self.name,
                version=self.version,
                execution_time_ms=elapsed_ms,
            )

        return ToolResult.success(
            data=out_data,
            tool_name=self.name,
            version=self.version,
            execution_time_ms=elapsed_ms,
        )

    async def _list_attachments(
        self,
        agent_context: AgentContext,
        t0: float,
    ) -> ToolResult:
        """Return all attachments for the current conversation session."""
        from src.shared.database import _get_session_maker  # noqa: PLC0415
        from src.shared.models.generated_file import GSageFile  # noqa: PLC0415
        from src.mcp_server.tenant_context import get_tenant_headers_or_none  # noqa: PLC0415

        tenant = get_tenant_headers_or_none()
        session_id = tenant.gsage_session_id if tenant else None

        async with _get_session_maker()() as db:
            stmt = (
                select(GSageFile)
                .where(
                    GSageFile.org_id == agent_context.org_id,
                    GSageFile.category == "attachment",
                    GSageFile.purged_at.is_(None),
                )
            )
            if session_id:
                stmt = stmt.where(GSageFile.session_id == session_id)
            else:
                stmt = stmt.where(GSageFile.user_id == agent_context.user_id)

            stmt = stmt.order_by(GSageFile.created_at.desc()).limit(50)
            result = await db.execute(stmt)
            rows = result.scalars().all()

        attachments = [
            {
                "file_id": str(row.id),
                "filename": row.filename,
                "content_type": row.content_type,
                "size_bytes": row.size_bytes,
                "download_path": (
                    f"/v1/orgs/{agent_context.org_id}/files/{row.id}/download"
                ),
            }
            for row in rows
        ]

        return ToolResult.success(
            data={"attachments": attachments, "count": len(attachments)},
            tool_name=self.name,
            version=self.version,
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )
