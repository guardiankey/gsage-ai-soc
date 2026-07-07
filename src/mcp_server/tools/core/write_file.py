"""gSage AI — WriteFile tool.

Allows the agent to create, edit, diff, and soft-delete text files
in the current conversation scope.  Works with Markdown, HTML, plain
text, CSV, JSON, YAML, and similar text-based formats.

Use ``read_file`` to inspect file contents after creation or editing.

Actions
-------
create  — Create a new text file (returns file_id)
edit    — Edit an existing file (full replace or line-range splice)
diff    — Compare two files (unified diff + optional HTML side-by-side)
delete  — Soft-delete a file (hidden from read_file listings)

Permissions: ``agents:run``
"""

from __future__ import annotations

import difflib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import ClassVar

from sqlalchemy import select

from src.mcp_server.tools.base import BaseTool, ToolResult, _tool_session_ctx
from src.mcp_server.tools.core._file_shared import (
    EXT_TO_MIME,
    MAX_FILE_BYTES,
    TEXT_MIME_PREFIXES,
    infer_content_type,
    is_text_content,
)
from src.shared.models.generated_file import GSageFile
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


async def _lookup_file(
    session,
    file_id: str,
    org_id: uuid.UUID,
) -> GSageFile | None:
    """Look up a GSageFile by id + org_id. Returns None if not found."""
    try:
        file_uuid = uuid.UUID(file_id)
    except ValueError:
        return None

    result = await session.execute(
        select(GSageFile).where(
            GSageFile.id == file_uuid,
            GSageFile.org_id == org_id,
        )
    )
    return result.scalar_one_or_none()


class WriteFileTool(BaseTool):
    """Create, edit, diff, and delete text files in the current conversation scope.

    Works with text-based formats: Markdown (.md), HTML (.html), plain text
    (.txt), CSV (.csv), JSON (.json), YAML (.yaml), and similar.

    Use ``read_file`` to inspect file contents after creation or editing.

    **Actions:**

    ``create``
      Create a new text file. Provide ``filename`` and ``content``.
      Optionally set ``content_type`` — if omitted, it is inferred from the
      file extension. Returns the new ``file_id``.

    ``edit``
      Edit an existing file identified by ``file_id``. Two strategies:

      - **full replace** (default): Provide ``new_content`` to replace the
        entire file content.
      - **line_range**: Provide ``line_start``, ``line_end`` (1-based,
        inclusive), and ``new_content`` to replace only those lines — useful
        for large files. The lines are spliced: content before line_start +
        new_content + content after line_end.

    ``diff``
      Compare two files line-by-line. Provide ``file_id_a`` (as ``file_id``)
      and ``file_id_b``. Returns a unified diff (``diff -u`` style) inline.
      Optionally set ``generate_html_diff`` to also create an HTML
      side-by-side diff file (returned as a new ``file_id`` for viewing).

    ``delete``
      Soft-delete a file identified by ``file_id``. The file is hidden from
      ``read_file`` listings but the data remains for audit purposes.

    ``rename``
      Rename a file identified by ``file_id``. Provide ``new_filename`` with
      the desired name and extension. If the extension changes, the
      ``content_type`` is updated accordingly via ``infer_content_type()``.

    ``copy``
      Duplicate a file identified by ``file_id``. A new file is created with
      the same content and a new ``file_id``. Optionally provide
      ``new_filename`` — if omitted, ``_copy`` is appended to the original
      name (before the extension).

    Permissions: ``agents:run``
    """

    name: ClassVar[str] = "write_file"
    version: ClassVar[str] = "1.1.0"
    summary: ClassVar[str] = (
        "Create, edit (line-range or full replace), diff (unified + HTML), "
        "rename, copy, or soft-delete text files (MD, HTML, TXT, CSV, JSON) "
        "in the current conversation scope."
    )
    category: ClassVar[str] = "file"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["agents:run"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "diff", "rename", "copy", "delete"],
                "description": (
                    "Action to perform: "
                    "create — create a new text file; "
                    "edit — edit an existing file (full replace or line range); "
                    "diff — compare two files line-by-line; "
                    "rename — change a file's name (and content_type if extension changes); "
                    "copy — duplicate a file (new file_id, same content); "
                    "delete — soft-delete a file (hidden from listings)."
                ),
            },
            # ── create params ──
            "filename": {
                "type": "string",
                "description": (
                    "File name with extension. Required for 'create'. "
                    "Used to infer content_type if not explicitly provided. "
                    "Examples: 'report.md', 'data.csv', 'notes.txt'."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Full text content for the new file. Required for 'create'."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "MIME type of the content. Optional — inferred from filename "
                    "extension if omitted. Examples: 'text/markdown', 'text/html', "
                    "'text/csv', 'text/plain'."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional human-readable description stored with the file "
                    "(for 'create' action)."
                ),
            },
            # ── edit / delete / diff / rename / copy params ──
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the file to operate on. Required for 'edit', "
                    "'rename', 'copy', 'delete', and for 'diff' as file_id_a "
                    "(when file_id_b is also provided). Obtain from a prior "
                    "'create' call or from 'read_file' (mode='list')."
                ),
            },
            # ── rename / copy params ──
            "new_filename": {
                "type": "string",
                "description": (
                    "New file name with extension. Required for 'rename'. "
                    "Optional for 'copy' — defaults to original name with "
                    "'_copy' suffix (before the extension). "
                    "Examples: 'v2-report.md', 'final-data.csv'."
                ),
            },
            # ── edit params ──
            "new_content": {
                "type": "string",
                "description": (
                    "Replacement text. For 'edit' with full replace: the new "
                    "full file content. For 'edit' with line_range: the text "
                    "to insert in place of lines [line_start, line_end]."
                ),
            },
            "line_start": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "First line to replace (1-based inclusive). "
                    "When provided with line_end, enables line_range editing "
                    "instead of full replace. Requires 'edit' action."
                ),
            },
            "line_end": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Last line to replace (1-based inclusive). "
                    "Must be >= line_start. "
                    "When provided with line_start, enables line_range editing."
                ),
            },
            # ── diff params ──
            "file_id_b": {
                "type": "string",
                "description": (
                    "UUID of the second file to compare. Required for 'diff' "
                    "action. file_id (or file_id_a) is used as the first file."
                ),
            },
            "diff_context_lines": {
                "type": "integer",
                "minimum": 0,
                "maximum": 20,
                "default": 3,
                "description": (
                    "Number of context lines around each diff hunk (unified diff). "
                    "Default 3. Set to 0 for minimal output, higher for more context."
                ),
            },
            "generate_html_diff": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When True, also generate an HTML side-by-side diff file "
                    "and store it as a new attachment (returned via html_diff_file_id). "
                    "Useful for presenting the diff to the user visually."
                ),
            },
        },
        "additionalProperties": False,
    }

    # ── execute ────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()

        session = _tool_session_ctx.get()
        if session is None:
            return self._failure(
                "INTERNAL_ERROR",
                "No DB session available.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        action: str = params["action"]

        if action == "create":
            return await self._create(agent_context, params, session, t0)
        if action == "edit":
            return await self._edit(agent_context, params, session, t0)
        if action == "diff":
            return await self._diff(agent_context, params, session, t0)
        if action == "rename":
            return await self._rename(agent_context, params, session, t0)
        if action == "copy":
            return await self._copy(agent_context, params, session, t0)
        if action == "delete":
            return await self._delete(agent_context, params, session, t0)

        return self._failure(
            "INVALID_ACTION",
            f"Unknown action: '{action}'",
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    # ── Action handlers ────────────────────────────────────────────────────

    async def _create(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        filename: str | None = (params.get("filename") or "").strip() or None
        content_str: str | None = params.get("content")
        content_type: str | None = (params.get("content_type") or "").strip() or None
        description: str | None = (params.get("description") or "").strip() or None

        if not filename:
            return self._failure(
                "PARAM_MISSING",
                "'filename' is required for create.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if content_str is None:
            return self._failure(
                "PARAM_MISSING",
                "'content' is required for create.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Infer content_type from extension if not provided
        if not content_type:
            content_type = infer_content_type(filename)

        # Validate text content type
        if not is_text_content(content_type):
            return self._failure(
                "INVALID_CONTENT_TYPE",
                f"Content type '{content_type}' is not a supported text format. "
                f"Supported prefixes: {TEXT_MIME_PREFIXES}",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        data = content_str.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            return self._failure(
                "CONTENT_TOO_LARGE",
                f"Content size ({len(data)} bytes) exceeds the maximum "
                f"({MAX_FILE_BYTES} bytes).",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        result = await self._store_file(
            data=data,
            filename=filename,
            content_type=content_type,
            agent_context=agent_context,
            session=session,
            description=description or None,
        )

        if result is None:
            return self._failure(
                "STORE_FAILED",
                "Failed to store file in MinIO.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        log.info(
            "write_file create: %s (%s, %d bytes) → %s",
            filename, content_type, len(data), result["file_id"],
        )

        return self._success(
            {**result, "action": "create"},
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    async def _edit(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        file_id: str | None = (params.get("file_id") or "").strip() or None
        new_content: str | None = params.get("new_content")
        line_start: int | None = params.get("line_start")
        line_end: int | None = params.get("line_end")

        if not file_id:
            return self._failure(
                "PARAM_MISSING",
                "'file_id' is required for edit.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if new_content is None:
            return self._failure(
                "PARAM_MISSING",
                "'new_content' is required for edit.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Look up the file
        row = await _lookup_file(session, file_id, agent_context.org_id)
        if row is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if row.purged_at is not None:
            return self._failure(
                "FILE_PURGED",
                f"File '{file_id}' has been deleted and cannot be edited.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not is_text_content(row.content_type):
            return self._failure(
                "NOT_TEXT_FILE",
                f"File '{row.filename}' is not a text file (type: {row.content_type}). "
                "write_file only supports text-based formats.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Load current content (needed for line_range strategy)
        loaded = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            session=session,
            max_bytes=MAX_FILE_BYTES,
        )
        if loaded is None:
            return self._failure(
                "LOAD_FAILED",
                f"Failed to load current content for file '{file_id}'.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        current_bytes: bytes = loaded["data"]
        size_before = len(current_bytes)

        # Determine edit strategy
        if line_start is not None and line_end is not None:
            # ── line_range strategy (same algorithm as wikijs_editor._edit_page) ──
            current_text = current_bytes.decode("utf-8")
            lines = current_text.split("\n")
            total_lines = len(lines)
            ls = max(1, line_start)
            le = min(total_lines, line_end)
            if ls > le:
                return self._failure(
                    "PARAM_INVALID",
                    f"line_start ({line_start}) must be <= line_end ({line_end}). "
                    f"File has {total_lines} lines.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )
            replacement_lines = new_content.split("\n")
            new_lines_list = lines[: ls - 1] + replacement_lines + lines[le:]
            new_bytes = "\n".join(new_lines_list).encode("utf-8")
            strategy = "line_range"
        else:
            # ── full replace strategy ──
            new_bytes = new_content.encode("utf-8")
            strategy = "full_replace"

        if len(new_bytes) > MAX_FILE_BYTES:
            return self._failure(
                "CONTENT_TOO_LARGE",
                f"Resulting content size ({len(new_bytes)} bytes) exceeds the "
                f"maximum ({MAX_FILE_BYTES} bytes).",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        result = await self._replace_file_content(
            file_id=file_id,
            data=new_bytes,
            agent_context=agent_context,
            session=session,
        )

        if result is None:
            return self._failure(
                "REPLACE_FAILED",
                f"Failed to update content for file '{file_id}'.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        log.info(
            "write_file edit: %s (%s) → %d → %d bytes",
            file_id, strategy, size_before, result["size_bytes"],
        )

        return self._success(
            {
                "action": "edit",
                "strategy": strategy,
                "file_id": result["file_id"],
                "filename": result["filename"],
                "content_type": result["content_type"],
                "size_bytes_before": size_before,
                "size_bytes_after": result["size_bytes"],
                "line_start": line_start,
                "line_end": line_end,
                "download_path": result["download_path"],
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    async def _diff(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        file_id_a: str | None = (params.get("file_id") or "").strip() or None
        file_id_b: str | None = (params.get("file_id_b") or "").strip() or None
        context_lines: int = int(params.get("diff_context_lines", 3))
        generate_html: bool = bool(params.get("generate_html_diff", False))

        if not file_id_a:
            return self._failure(
                "PARAM_MISSING",
                "'file_id' (file A) is required for diff.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not file_id_b:
            return self._failure(
                "PARAM_MISSING",
                "'file_id_b' (file B) is required for diff.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if file_id_a == file_id_b:
            return self._failure(
                "PARAM_INVALID",
                "file_id and file_id_b must be different files to diff.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Load both files
        loaded_a = await self._load_file(
            file_id=file_id_a,
            org_id=str(agent_context.org_id),
            session=session,
            max_bytes=MAX_FILE_BYTES,
        )
        loaded_b = await self._load_file(
            file_id=file_id_b,
            org_id=str(agent_context.org_id),
            session=session,
            max_bytes=MAX_FILE_BYTES,
        )

        if loaded_a is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File A '{file_id_a}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if loaded_b is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File B '{file_id_b}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Verify both are text
        if not is_text_content(loaded_a["content_type"]):
            return self._failure(
                "NOT_TEXT_FILE",
                f"File A '{loaded_a['filename']}' is not a text file "
                f"(type: {loaded_a['content_type']}).",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not is_text_content(loaded_b["content_type"]):
            return self._failure(
                "NOT_TEXT_FILE",
                f"File B '{loaded_b['filename']}' is not a text file "
                f"(type: {loaded_b['content_type']}).",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        text_a = loaded_a["data"].decode("utf-8")
        text_b = loaded_b["data"].decode("utf-8")
        lines_a = text_a.splitlines(keepends=True)
        lines_b = text_b.splitlines(keepends=True)

        # Unified diff
        diff_lines = list(
            difflib.unified_diff(
                lines_a,
                lines_b,
                fromfile=loaded_a["filename"],
                tofile=loaded_b["filename"],
                n=context_lines,
            )
        )
        unified_diff_str = "".join(diff_lines)

        # Statistics
        added = sum(
            1 for l in diff_lines if l.startswith("+") and not l.startswith("+++")
        )
        removed = sum(
            1 for l in diff_lines if l.startswith("-") and not l.startswith("---")
        )
        changed_hunks = sum(1 for l in diff_lines if l.startswith("@@"))

        result_data: dict = {
            "action": "diff",
            "file_a": {"file_id": file_id_a, "filename": loaded_a["filename"]},
            "file_b": {"file_id": file_id_b, "filename": loaded_b["filename"]},
            "lines_added": added,
            "lines_removed": removed,
            "hunks": changed_hunks,
            "unified_diff": unified_diff_str if unified_diff_str else "(no differences)",
        }

        # Optional HTML side-by-side diff
        if generate_html:
            html_diff = difflib.HtmlDiff(wrapcolumn=100).make_file(
                lines_a,
                lines_b,
                fromdesc=loaded_a["filename"],
                todesc=loaded_b["filename"],
                context=True,
                numlines=context_lines,
            )
            html_bytes = html_diff.encode("utf-8")
            html_result = await self._store_file(
                data=html_bytes,
                filename=(
                    f"diff_{_safe_basename(loaded_a['filename'])}"
                    f"_vs_{_safe_basename(loaded_b['filename'])}.html"
                ),
                content_type="text/html",
                agent_context=agent_context,
                session=session,
                description=(
                    f"Side-by-side diff: {loaded_a['filename']} vs {loaded_b['filename']}"
                ),
            )
            if html_result:
                result_data["html_diff_file_id"] = html_result["file_id"]
                result_data["html_diff_download_path"] = html_result["download_path"]

        log.info(
            "write_file diff: %s ↔ %s → +%d/-%d in %d hunks",
            loaded_a["filename"], loaded_b["filename"], added, removed, changed_hunks,
        )

        return self._success(
            result_data,
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    async def _delete(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        file_id: str | None = (params.get("file_id") or "").strip() or None

        if not file_id:
            return self._failure(
                "PARAM_MISSING",
                "'file_id' is required for delete.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        row = await _lookup_file(session, file_id, agent_context.org_id)
        if row is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if row.purged_at is not None:
            return self._failure(
                "ALREADY_DELETED",
                f"File '{row.filename}' ({file_id}) has already been deleted.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Soft-delete: set purged_at only. The Celery task purge_expired_files
        # handles MinIO cleanup later. read_file already filters purged_at.is_(None).
        row.purged_at = datetime.now(timezone.utc)
        await session.commit()

        log.info(
            "write_file delete: %s (%s) — soft-deleted",
            row.filename, file_id,
        )

        return self._success(
            {
                "action": "delete",
                "file_id": str(row.id),
                "filename": row.filename,
                "deleted": True,
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    async def _rename(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        file_id: str | None = (params.get("file_id") or "").strip() or None
        new_filename: str | None = (params.get("new_filename") or "").strip() or None

        if not file_id:
            return self._failure(
                "PARAM_MISSING",
                "'file_id' is required for rename.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not new_filename:
            return self._failure(
                "PARAM_MISSING",
                "'new_filename' is required for rename.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        row = await _lookup_file(session, file_id, agent_context.org_id)
        if row is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if row.purged_at is not None:
            return self._failure(
                "FILE_PURGED",
                f"File '{row.filename}' has been deleted and cannot be renamed.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        old_filename = row.filename
        row.filename = new_filename

        # Update content_type if the extension changed
        new_content_type = infer_content_type(new_filename)
        if new_content_type != row.content_type:
            row.content_type = new_content_type
            content_type_updated = True
        else:
            content_type_updated = False

        await session.commit()

        log.info(
            "write_file rename: %s → %s (content_type updated=%s)",
            old_filename, new_filename, content_type_updated,
        )

        return self._success(
            {
                "action": "rename",
                "file_id": str(row.id),
                "old_filename": old_filename,
                "new_filename": new_filename,
                "content_type": row.content_type,
                "content_type_updated": content_type_updated,
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )

    async def _copy(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        import os

        file_id: str | None = (params.get("file_id") or "").strip() or None
        new_filename: str | None = (params.get("new_filename") or "").strip() or None

        if not file_id:
            return self._failure(
                "PARAM_MISSING",
                "'file_id' is required for copy.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        row = await _lookup_file(session, file_id, agent_context.org_id)
        if row is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if row.purged_at is not None:
            return self._failure(
                "FILE_PURGED",
                f"File '{row.filename}' has been deleted and cannot be copied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Load current content
        loaded = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            session=session,
            max_bytes=MAX_FILE_BYTES,
        )
        if loaded is None:
            return self._failure(
                "LOAD_FAILED",
                f"Failed to load content for file '{file_id}'.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Generate name if not provided
        if not new_filename:
            base, ext = os.path.splitext(row.filename)
            new_filename = f"{base}_copy{ext}"

        # Infer content_type from the new filename
        new_content_type = infer_content_type(new_filename)

        # Store as a new file
        result = await self._store_file(
            data=loaded["data"],
            filename=new_filename,
            content_type=new_content_type,
            agent_context=agent_context,
            session=session,
            description=f"Copy of {row.filename} ({file_id})",
        )

        if result is None:
            return self._failure(
                "STORE_FAILED",
                "Failed to store copied file in MinIO.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        log.info(
            "write_file copy: %s → %s (%s, %d bytes)",
            row.filename, new_filename, new_content_type, loaded["size_bytes"],
        )

        return self._success(
            {
                "action": "copy",
                "original_file_id": file_id,
                **result,
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )


def _safe_basename(filename: str) -> str:
    """Return a filesystem-safe base name for use in generated filenames."""
    # Strip extension and replace problematic characters
    import os
    base = os.path.splitext(filename)[0]
    # Replace anything that isn't alphanumeric, dash, underscore, or dot
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in base)
    return safe or "file"
