"""gSage AI — WriteFile tool.

Allows the agent to create, edit, insert_file, diff, and soft-delete
text files in the current conversation scope.  Works with Markdown,
HTML, plain text, CSV, JSON, YAML, and similar text-based formats.

Use ``read_file`` to inspect file contents after creation or editing.

Actions
-------
create      — Create a new text file (returns file_id)
edit        — Edit an existing file (full replace or line-range splice)
insert_file — Insert the entire content of one file into another at a line
diff        — Compare two files (unified diff + optional HTML side-by-side)
delete      — Soft-delete a file (hidden from read_file listings)

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
    """Create, edit, insert_file, append, diff, and delete text files in the current conversation scope.

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
      - **splice** / **line_range**: Provide ``line_start`` with
        ``line_count`` (preferred) or ``line_end`` (legacy) to remove N
        lines starting at ``line_start`` and insert ``new_content`` at
        that position — a textual splice. ``line_count=0`` inserts
        without removing. ``new_content=""`` removes without inserting.
        All edit operations (replace, insert, delete, expand, shrink)
        are special cases of this single splice primitive.

    ``insert_file``
      Insert the **entire content** of a source file into a target file at
      a specific line. Provide ``source_file_id`` (the file whose content
      will be inserted), ``file_id`` (the target file), and ``line_start``
      (1-based line where insertion begins). Optionally set ``line_count``
      to remove N lines from the target before inserting (default 0 = pure
      insert). Both files must be accessible and text-based. Source and
      target must be different files.

      Example — insert file A's content at line 15 of file B (pure insert):
      ``action="insert_file", source_file_id="<A>", file_id="<B>", line_start=15``

    ``append``
      Add text to the end of an existing file. Provide ``file_id`` and
      ``content``. A newline separator is inserted automatically when the
      existing content does not already end with one. Useful for building
      files incrementally (e.g. accumulating summaries from chunked reads).

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
    version: ClassVar[str] = "1.3.0"
    summary: ClassVar[str] = (
        "Create, edit (line-range or full replace), insert_file (merge files), "
        "append, diff (unified + HTML), rename, copy, or soft-delete text files "
        "(MD, HTML, TXT, CSV, JSON) in the current conversation scope."
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
                "enum": ["create", "edit", "insert_file", "diff", "rename", "copy", "delete", "append"],
                "description": (
                    "Action to perform: "
                    "create — create a new text file; "
                    "edit — edit an existing file (full replace or line range); "
                    "insert_file — insert the entire content of a source file into a target file at a line; "
                    "diff — compare two files line-by-line; "
                    "rename — change a file's name (and content_type if extension changes); "
                    "copy — duplicate a file (new file_id, same content); "
                    "delete — soft-delete a file (hidden from listings); "
                    "append — add content to the end of an existing file."
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
                    "Text content. Required for 'create' (full file content) "
                    "and 'append' (text to add to the end of the file)."
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
            # ── insert_file params ──
            "source_file_id": {
                "type": "string",
                "description": (
                    "UUID of the source file whose entire content will be "
                    "inserted. Required for 'insert_file'. The file must be "
                    "text-based and accessible (same org). Must be different "
                    "from the target file_id."
                ),
            },
            # ── edit / delete / diff / rename / copy / insert_file params ──
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the file to operate on. Required for 'edit', "
                    "'insert_file' (as target), 'rename', 'copy', 'delete', "
                    "and for 'diff' as file_id_a (when file_id_b is also "
                    "provided). Obtain from a prior 'create' call or from "
                    "'read_file' (mode='list')."
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
                    "Replacement text for the splice: remove line_count lines "
                    "starting at line_start, insert this text at that position. "
                    "When empty (''), the splice inserts 0 replacement lines — "
                    "pure deletion when line_count > 0, no-op when line_count = 0. "
                    "For 'edit' with full replace (no line_start): the new full file content."
                ),
            },
            "line_start": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "First line to operate on (1-based inclusive). "
                    "When provided with line_count or line_end, enables splice/line_range "
                    "editing instead of full replace. Required for 'edit' (splice mode) "
                    "and 'insert_file' actions."
                ),
            },
            "line_count": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Number of lines to REMOVE starting at line_start. "
                    "0 = pure insert (remove nothing, insert before line_start). "
                    "For 'edit': together with new_content this forms a splice. "
                    "For 'insert_file': together with source_file_id this forms a splice. "
                    "Mutually exclusive with line_end. "
                    "Preferred over line_end for new code."
                ),
            },
            "line_end": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Last line to replace (1-based inclusive, ABSOLUTE line number). "
                    "Legacy parameter — prefer line_count for new code. "
                    "Mutually exclusive with line_count. "
                    "When provided with line_start, enables line_range editing."
                ),
            },
            "expected_revision": {
                "type": "string",
                "description": (
                    "Optional opaque revision token from a prior read_file response. "
                    "When provided, the edit is rejected with REVISION_CONFLICT if "
                    "the file has been modified since that read. "
                    "Prevents lost updates from concurrent edits."
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
        if action == "insert_file":
            return await self._insert_file(agent_context, params, session, t0)
        if action == "diff":
            return await self._diff(agent_context, params, session, t0)
        if action == "rename":
            return await self._rename(agent_context, params, session, t0)
        if action == "copy":
            return await self._copy(agent_context, params, session, t0)
        if action == "delete":
            return await self._delete(agent_context, params, session, t0)
        if action == "append":
            return await self._append(agent_context, params, session, t0)

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
        line_start: int | None = _coerce_int(params.get("line_start"))
        line_end: int | None = _coerce_int(params.get("line_end"))
        line_count: int | None = _coerce_int(params.get("line_count"))
        expected_revision: str | None = (params.get("expected_revision") or "").strip() or None

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

        # ── Mutual exclusion: line_count vs line_end ──────────────────
        has_line_count = line_count is not None
        has_line_end = line_end is not None
        if has_line_count and has_line_end:
            return self._failure(
                "INVALID_PARAMS",
                "'line_count' and 'line_end' are mutually exclusive. "
                "Use 'line_count' for new code.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        # For splice/line_range mode, at least one is needed alongside line_start
        is_splice_mode = line_start is not None and (has_line_count or has_line_end)

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

        # ── Revision check (optimistic concurrency) ───────────────────
        if expected_revision:
            current_revision = row.updated_at.isoformat() if row.updated_at else None
            if current_revision is None or current_revision != expected_revision:
                return self._failure(
                    "REVISION_CONFLICT",
                    f"File was modified since last read. "
                    f"Expected revision {expected_revision}, "
                    f"current is {current_revision or 'unknown'}. "
                    f"Re-read the file to get the latest content.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )

        # Load current content (needed for splice/line_range strategy)
        loaded = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id) if agent_context.user_id else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
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
        if is_splice_mode:
            # ── Splice / line_range strategy ──────────────────────────
            current_text = current_bytes.decode("utf-8")
            # Detect line separator (LF vs CRLF)
            line_sep = _detect_line_separator(current_text)
            # Logical lines (see SPEC Section 0.1)
            lines = current_text.splitlines()
            total_lines = len(lines)
            assert line_start is not None  # guarded by is_splice_mode
            ls = max(1, line_start)

            if has_line_count:
                if line_count < 0:
                    return self._failure(
                        "INVALID_PARAMS",
                        "'line_count' must be >= 0.",
                        execution_time_ms=int((time.monotonic() - t0) * 1000),
                    )
                if ls > total_lines + 1:
                    return self._failure(
                        "LINE_OUT_OF_RANGE",
                        f"line_start ({line_start}) out of range. "
                        f"File has {total_lines} lines. "
                        f"Use line_start={total_lines + 1} to append.",
                        execution_time_ms=int((time.monotonic() - t0) * 1000),
                    )
                le = ls - 1 + line_count  # 0 → le = ls-1 (insert mode)
            else:
                # Legacy line_end mode
                assert line_end is not None  # guarded by has_line_end
                le = min(total_lines, line_end)
                if ls > le:
                    return self._failure(
                        "PARAM_INVALID",
                        f"line_start ({line_start}) must be <= line_end ({line_end}). "
                        f"File has {total_lines} lines. "
                        f"Use line_count=0 for insert mode.",
                        execution_time_ms=int((time.monotonic() - t0) * 1000),
                    )

            # Splice: remove lines [ls-1 : le], insert replacement_lines
            # new_content="" → 0 replacement lines (pure delete when line_count > 0)
            if new_content == "":
                replacement_lines: list[str] = []
            else:
                replacement_lines = new_content.splitlines()

            new_lines_list = lines[: ls - 1] + replacement_lines + lines[le:]
            new_text = line_sep.join(new_lines_list)
            # Preserve trailing newline if original had one
            if current_text.endswith("\n") and not new_text.endswith("\n"):
                new_text += "\n"
            elif current_text.endswith("\r\n") and not new_text.endswith("\r\n"):
                new_text += "\r\n"
            new_bytes = new_text.encode("utf-8")
            strategy = "splice" if has_line_count else "line_range"
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
            user_id=str(agent_context.user_id) if agent_context.user_id else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            session=session,
            max_bytes=MAX_FILE_BYTES,
        )
        loaded_b = await self._load_file(
            file_id=file_id_b,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id) if agent_context.user_id else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
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
            user_id=str(agent_context.user_id) if agent_context.user_id else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
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

    async def _append(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        """Append text to the end of an existing file.

        Requires ``file_id`` and ``content`` (the text to append).
        A newline (``\\n``) is automatically inserted between the existing
        content and the appended text when the existing content does not
        already end with one — this ensures each append starts on its own
        line.
        """
        file_id: str | None = (params.get("file_id") or "").strip() or None
        content_str: str | None = params.get("content")

        if not file_id:
            return self._failure(
                "PARAM_MISSING",
                "'file_id' is required for append.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if content_str is None:
            return self._failure(
                "PARAM_MISSING",
                "'content' is required for append.",
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
                f"File '{row.filename}' has been deleted and cannot be appended to.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not is_text_content(row.content_type):
            return self._failure(
                "NOT_TEXT_FILE",
                f"File '{row.filename}' is not a text file (type: {row.content_type}). "
                "write_file only supports text-based formats.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # Load current content
        loaded = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id) if agent_context.user_id else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
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
        current_text = current_bytes.decode("utf-8")

        # Ensure a newline separator between existing content and appended text
        separator = "" if current_text.endswith("\n") else "\n"
        new_text = current_text + separator + content_str
        new_bytes = new_text.encode("utf-8")

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
                f"Failed to append content to file '{file_id}'.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        log.info(
            "write_file append: %s → %d + %d = %d bytes",
            file_id, size_before, len(content_str),
            result["size_bytes"],
        )

        return self._success(
            {
                "action": "append",
                "file_id": result["file_id"],
                "filename": result["filename"],
                "content_type": result["content_type"],
                "size_bytes_before": size_before,
                "size_bytes_appended": len(content_str.encode("utf-8")),
                "size_bytes_after": result["size_bytes"],
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )


    async def _insert_file(
        self,
        agent_context: AgentContext,
        params: dict,
        session,
        t0: float,
    ) -> ToolResult:
        """Insert the entire content of a source file into a target file at a line.

        Uses the same splice logic as ``_edit``, but the replacement text
        comes from a source file instead of an inline ``new_content`` string.
        """
        source_file_id: str | None = (params.get("source_file_id") or "").strip() or None
        target_file_id: str | None = (params.get("file_id") or "").strip() or None
        line_start: int | None = _coerce_int(params.get("line_start"))
        line_count: int | None = _coerce_int(params.get("line_count"))

        # ── Validate required params ─────────────────────────────────
        if not source_file_id:
            return self._failure(
                "PARAM_MISSING",
                "'source_file_id' is required for insert_file.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not target_file_id:
            return self._failure(
                "PARAM_MISSING",
                "'file_id' (target) is required for insert_file.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if source_file_id == target_file_id:
            return self._failure(
                "PARAM_INVALID",
                "source_file_id and file_id must be different files.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if line_start is None:
            return self._failure(
                "PARAM_MISSING",
                "'line_start' is required for insert_file.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if line_count is not None and line_count < 0:
            return self._failure(
                "INVALID_PARAMS",
                "'line_count' must be >= 0.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── Look up both files ───────────────────────────────────────
        source_row = await _lookup_file(session, source_file_id, agent_context.org_id)
        if source_row is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"Source file '{source_file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if source_row.purged_at is not None:
            return self._failure(
                "FILE_PURGED",
                f"Source file '{source_row.filename}' has been deleted.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not is_text_content(source_row.content_type):
            return self._failure(
                "NOT_TEXT_FILE",
                f"Source file '{source_row.filename}' is not a text file "
                f"(type: {source_row.content_type}).",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        target_row = await _lookup_file(session, target_file_id, agent_context.org_id)
        if target_row is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"Target file '{target_file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if target_row.purged_at is not None:
            return self._failure(
                "FILE_PURGED",
                f"Target file '{target_row.filename}' has been deleted.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        if not is_text_content(target_row.content_type):
            return self._failure(
                "NOT_TEXT_FILE",
                f"Target file '{target_row.filename}' is not a text file "
                f"(type: {target_row.content_type}).",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── Load source file content ──────────────────────────────────
        source_loaded = await self._load_file(
            file_id=source_file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id) if agent_context.user_id else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            session=session,
            max_bytes=MAX_FILE_BYTES,
        )
        if source_loaded is None:
            return self._failure(
                "LOAD_FAILED",
                f"Failed to load source file '{source_file_id}'.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        source_text = source_loaded["data"].decode("utf-8")

        # ── Load target file content ──────────────────────────────────
        target_loaded = await self._load_file(
            file_id=target_file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id) if agent_context.user_id else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            session=session,
            max_bytes=MAX_FILE_BYTES,
        )
        if target_loaded is None:
            return self._failure(
                "LOAD_FAILED",
                f"Failed to load target file '{target_file_id}'.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        target_bytes: bytes = target_loaded["data"]
        size_before = len(target_bytes)
        target_text = target_bytes.decode("utf-8")

        # ── Splice: same logic as _edit ───────────────────────────────
        line_sep = _detect_line_separator(target_text)
        lines = target_text.splitlines()
        total_lines = len(lines)
        assert line_start is not None  # validated above
        ls = max(1, line_start)
        lc = line_count if line_count is not None else 0

        if ls > total_lines + 1:
            return self._failure(
                "LINE_OUT_OF_RANGE",
                f"line_start ({line_start}) out of range. "
                f"Target file has {total_lines} lines. "
                f"Use line_start={total_lines + 1} to append at end.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        le = ls - 1 + lc  # 0-based index after removal
        source_lines = source_text.splitlines()

        new_lines_list = lines[: ls - 1] + source_lines + lines[le:]
        new_text = line_sep.join(new_lines_list)
        # Preserve trailing newline if target had one
        if target_text.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
        elif target_text.endswith("\r\n") and not new_text.endswith("\r\n"):
            new_text += "\r\n"
        new_bytes = new_text.encode("utf-8")

        # ── Validate resulting size ───────────────────────────────────
        if len(new_bytes) > MAX_FILE_BYTES:
            return self._failure(
                "CONTENT_TOO_LARGE",
                f"Resulting content size ({len(new_bytes)} bytes) exceeds the "
                f"maximum ({MAX_FILE_BYTES} bytes).",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── Persist ───────────────────────────────────────────────────
        result = await self._replace_file_content(
            file_id=target_file_id,
            data=new_bytes,
            agent_context=agent_context,
            session=session,
        )

        if result is None:
            return self._failure(
                "REPLACE_FAILED",
                f"Failed to update target file '{target_file_id}'.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        log.info(
            "write_file insert_file: src=%s → dst=%s @line %d, "
            "%d → %d bytes (%d source lines, %d removed)",
            source_file_id, target_file_id, line_start,
            size_before, result["size_bytes"],
            len(source_lines), lc,
        )

        return self._success(
            {
                "action": "insert_file",
                "source_file_id": source_file_id,
                "source_filename": source_row.filename,
                "target_file_id": result["file_id"],
                "target_filename": result["filename"],
                "content_type": result["content_type"],
                "size_bytes_before": size_before,
                "size_bytes_after": result["size_bytes"],
                "source_lines": len(source_lines),
                "target_lines_before": total_lines,
                "line_start": line_start,
                "line_count": lc,
                "download_path": result["download_path"],
            },
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        )


def _coerce_int(value: object) -> int | None:
    """Coerce *value* to ``int``, accepting ``int``, ``str``, or ``float``.

    Returns ``None`` when *value* is ``None`` or cannot be converted.
    LLM tool-calling interfaces may serialize integers as strings, so we
    accept both forms transparently.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        # Only accept whole-number floats (e.g. 68.0, not 68.5)
        if value == int(value):
            return int(value)
        return None
    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    return None


def _safe_basename(filename: str) -> str:
    """Return a filesystem-safe base name for use in generated filenames."""
    # Strip extension and replace problematic characters
    import os
    base = os.path.splitext(filename)[0]
    # Replace anything that isn't alphanumeric, dash, underscore, or dot
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in base)
    return safe or "file"


def _detect_line_separator(text: str) -> str:
    """Detect the dominant line separator in *text*.

    Returns ``\\r\\n`` if any CRLF is found, otherwise ``\\n``.
    For mixed-endings files, CRLF takes precedence to avoid corrupting
    Windows-style lines.
    """
    if "\r\n" in text:
        return "\r\n"
    return "\n"
