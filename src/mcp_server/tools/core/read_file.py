"""gSage AI — ReadFile tool.

Allows the agent to read the contents of a text file attached to
the current chat session, list all attachments, navigate Markdown
sections, or search with regex.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import ClassVar

from sqlalchemy import select

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Shared file-tool constants and helpers (text detection, MIME, size limits).
from src.mcp_server.tools.core._file_shared import (
    TEXT_MIME_PREFIXES,
    MAX_FILE_BYTES,
    is_text_content,
)

log = logging.getLogger(__name__)

# Default maximum lines returned for text content (no filters applied).
_DEFAULT_MAX_LINES = 200

# Markdown content types and extensions for section-aware features.
_MD_CONTENT_TYPES: tuple[str, ...] = ("text/markdown",)
_MD_EXTENSIONS: tuple[str, ...] = (".md", ".mdx", ".markdown")

# Regex to match ATX headings (1-6 # followed by space and title).
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _is_markdown(content_type: str, filename: str) -> bool:
    """Return True if the file should be treated as Markdown."""
    ct = content_type.lower().split(";")[0].strip()
    if ct in _MD_CONTENT_TYPES:
        return True
    return filename.lower().endswith(_MD_EXTENSIONS)


def _parse_md_sections(lines: list[str]) -> list[dict]:
    """Parse ATX headings from *lines* and return a flat list of sections.

    Each entry: {"level": int, "title": str, "line": int (1-based), "path": str}.
    "path" is the breadcrumb path using ">" separators (e.g. "Setup > Prerequisites").
    """
    flat: list[dict] = []
    stack: list[dict] = []

    for i, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        title = m.group(2).strip()

        # Build breadcrumb path from ancestor chain
        path_parts = [a["title"] for a in stack if a["level"] < level]
        path_parts.append(title)
        path = " > ".join(path_parts)

        entry: dict = {"level": level, "title": title, "line": i, "path": path}

        # Pop stack until we find a parent heading (strictly lower level)
        while stack and stack[-1]["level"] >= level:
            stack.pop()

        stack.append(entry)
        flat.append(entry)

    return flat


def _resolve_section(
    flat: list[dict],
    *,
    section_path: str | None = None,
    section_title: str | None = None,
    section_level: int | None = None,
) -> tuple[dict | None, str | None]:
    """Find a section in *flat* by path or title.

    Returns (entry, error_message).  One of them is always None.
    """
    if section_path:
        # Exact path match (case-insensitive)
        path_lower = section_path.lower().strip()
        for entry in flat:
            if entry["path"].lower() == path_lower:
                return entry, None
        # Build suggestions for close matches
        import difflib
        candidates = difflib.get_close_matches(
            path_lower, [e["path"].lower() for e in flat], n=5, cutoff=0.4
        )
        suggestion = ""
        if candidates:
            suggestion = f" Did you mean: {', '.join(repr(c) for c in candidates)}?"
        return None, f"Section path {section_path!r} not found.{suggestion}"

    if section_title:
        title_lower = section_title.lower().strip()
        matches = [
            e for e in flat
            if e["title"].lower() == title_lower
            and (section_level is None or e["level"] == section_level)
        ]
        if len(matches) == 0:
            return None, f"Section title {section_title!r} not found."
        if len(matches) == 1:
            return matches[0], None
        # Ambiguous: multiple matches at different levels
        paths = [m["path"] for m in matches]
        return None, (
            f"Ambiguous section title {section_title!r}. "
            f"Matching paths: {', '.join(repr(p) for p in paths)}. "
            f"Use 'section_path' to pick one, or 'section_level' to disambiguate."
        )

    return None, "Provide 'section_path' or 'section_title'."


def _section_end_line(flat: list[dict], entry: dict, total_lines: int) -> int:
    """Return the last line of the section starting at *entry*.

    The section ends at the line before the next heading of equal or higher level,
    or at EOF if no such heading exists.
    """
    level = entry["level"]
    for other in flat:
        if other["line"] > entry["line"] and other["level"] <= level:
            return other["line"] - 1
    return total_lines


class ReadFileTool(BaseTool):
    """
    Read File — read a text file, list attachments, or navigate Markdown sections.

    **Mode — content** (``file_id`` provided, default):
    Returns the content of a text-based attachment (plain text, JSON, CSV,
    YAML, script files, Markdown, etc.) together with file metadata and hashes
    (MD5 / SHA1 / SHA256).

    For **binary files** (images, PDFs, ZIPs, executables, ...) the content
    is never returned — use the tool only to obtain metadata and hashes.
    To read the actual content of a binary file use a dedicated analysis
    tool (e.g. ``pdf_analyzer``, ``eml_analyzer``).

    Text content is always filtered before being returned, so large files
    do not overflow the context:

    - **Line range**: ``start_line`` / ``end_line`` (1-indexed, inclusive).
      Defaults to the first ``max_lines`` lines of the file.
    - **Tail**: ``tail_lines`` reads the last N lines instead of the first.
    - **Grep**: ``grep`` with ``grep_mode`` (``"substring"`` or ``"regex"``),
      ``grep_case_sensitive``, and ``grep_context`` for surrounding lines.
    - **max_lines**: hard cap on the number of lines returned (default 200,
      max 1000).  Applied last, after all other filters.

    When neither ``start_line`` / ``end_line`` nor ``grep`` nor ``tail_lines``
    is provided the first 200 lines are returned (or fewer if the file is smaller).
    Call again with different ``start_line`` / ``end_line`` to page through
    a large file.

    **Mode — toc** (``file_id`` + ``mode: "toc"``):
    Returns a flat list of Markdown headings with level, title, line number,
    and breadcrumb path.  Only works for Markdown files.

    **Mode — section** (``file_id`` + ``mode: "section"``):
    Returns the content of a specific Markdown section identified by
    ``section_path`` or ``section_title``.  The section's content is then
    processed through the normal content-mode pipeline (grep, max_lines, etc.).

    **Mode — list** (``file_id`` omitted):
    Returns all attachments for the current conversation session with
    filename, content_type, size_bytes, and ``download_path`` for each.

    Permission: ``agents:run``
    """

    name: ClassVar[str] = "read_file"
    version: ClassVar[str] = "3.0.0"
    summary: ClassVar[str] = (
        "Read a text file attachment, list attachments, navigate Markdown "
        "sections, or search with regex — from the current conversation."
    )
    category: ClassVar[str] = "file"
    core_tool: ClassVar[bool] = False
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
            "mode": {
                "type": "string",
                "description": (
                    "Read mode. 'content' (default) — line-based read with optional "
                    "grep/tail/encoding. 'toc' — list Markdown headings (Markdown only). "
                    "'section' — read a specific Markdown section by path or title."
                ),
            },
            "section_path": {
                "type": "string",
                "description": (
                    "Breadcrumb path to a Markdown section, e.g. "
                    "'Setup > Prerequisites > Docker'. Used with mode='section'. "
                    "Obtain valid paths from a prior mode='toc' call."
                ),
            },
            "section_title": {
                "type": "string",
                "description": (
                    "Heading title of a Markdown section. Used with mode='section' "
                    "as an alternative to section_path. Use section_level to disambiguate."
                ),
            },
            "section_level": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "description": (
                    "Heading level (1-6) to disambiguate section_title matches."
                ),
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "First line to return (1-indexed, inclusive). "
                    "Defaults to 1. Ignored when tail_lines is set. "
                    "Mutually exclusive with 'offset'/'limit'. Text files only."
                ),
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Last line to return (1-indexed, inclusive). "
                    "Defaults to start_line + max_lines - 1. "
                    "Mutually exclusive with 'offset'/'limit'. Text files only."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "0-indexed start line for paginated reads (like array index). "
                    "Maps to start_line = offset + 1. "
                    "Mutually exclusive with start_line/end_line. "
                    "Use with 'limit': offset=8, limit=8 returns lines 9-16 (8 lines)."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": (
                    "Maximum number of lines to return starting from 'offset' — "
                    "a COUNT, not an absolute line number. "
                    "Maps to end_line = offset + limit. "
                    "Subject to max_lines cap. "
                    "Only valid when 'offset' is also provided."
                ),
            },
            "tail_lines": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Read the last N lines of the file. "
                    "When set, start_line/end_line are ignored. Text files only."
                ),
            },
            "grep": {
                "type": "string",
                "description": (
                    "Return only lines matching this pattern. "
                    "By default a case-insensitive substring match. "
                    "Use grep_mode='regex' for regular expressions. "
                    "Applied after line-range/tail filtering."
                ),
            },
            "grep_mode": {
                "type": "string",
                "description": (
                    "Match mode for 'grep'. 'substring' (default) — plain-text "
                    "case-insensitive containment. 'regex' — Python re.search pattern."
                ),
            },
            "grep_case_sensitive": {
                "type": "boolean",
                "description": (
                    "When true, grep matching is case-sensitive. Default false."
                ),
            },
            "grep_context": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Number of context lines to include before and after each "
                    "grep match (like grep -C). Default 0. Overlapping context "
                    "blocks are merged."
                ),
            },
            "max_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": (
                    "Maximum number of lines to return (default 200, max 1000). "
                    "Applied last, after all other filters."
                ),
            },
            "encoding": {
                "type": "string",
                "description": (
                    "Text encoding for decoding the file bytes. "
                    "Default 'utf-8'. Falls back to latin-1 on error when using "
                    "the default encoding; explicit encodings return an error on failure."
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

        # ── Load file ────────────────────────────────────────────────────
        result = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=MAX_FILE_BYTES,
        )

        if result is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or access denied.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
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
        if not is_text_content(content_type):
            return self._success(
                {
                    **base_meta,
                    "content_note": (
                        "Binary file — content not returned. "
                        "Use a dedicated analysis tool (pdf_analyzer, eml_analyzer, etc.) "
                        "to inspect the file contents."
                    ),
                },
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── Text: decode ──────────────────────────────────────────────────
        encoding: str = params.get("encoding") or "utf-8"
        try:
            raw_text = data.decode(encoding, errors="replace")
        except LookupError:
            return self._failure(
                "INVALID_PARAMS",
                f"Unknown encoding {encoding!r}. Use a valid Python encoding name.",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception:
            if encoding == "utf-8":
                # Keep backward-compatible fallback for default encoding
                raw_text = data.decode("latin-1", errors="replace")
            else:
                return self._failure(
                    "CONVERSION_ERROR",
                    f"Failed to decode {filename!r} with encoding {encoding!r}.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )

        all_lines = raw_text.splitlines()
        total_lines = len(all_lines)
        total_words = len(raw_text.split())
        total_chars = len(raw_text)

        mode: str = params.get("mode") or "content"

        # ── TOC mode: return Markdown heading list ────────────────────────
        if mode == "toc":
            if not _is_markdown(content_type, filename):
                return self._failure(
                    "UNSUPPORTED_FORMAT",
                    f"TOC mode only available for Markdown files. "
                    f"{filename!r} is not Markdown.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )
            flat = _parse_md_sections(all_lines)
            log.info("read_file: toc for %s — %d headings", filename, len(flat))
            return self._success(
                {
                    **base_meta,
                    "mode": "toc",
                    "total_headings": len(flat),
                    "sections": flat,
                },
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )

        # ── Section mode: resolve section, then fall through ──────────────
        section_applied = False
        if mode == "section":
            if not _is_markdown(content_type, filename):
                return self._failure(
                    "UNSUPPORTED_FORMAT",
                    f"Section mode only available for Markdown files. "
                    f"{filename!r} is not Markdown.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )
            section_path: str | None = params.get("section_path") or None
            section_title: str | None = params.get("section_title") or None
            section_level: int | None = params.get("section_level") or None

            if not section_path and not section_title:
                return self._failure(
                    "INVALID_PARAMS",
                    "Provide 'section_path' or 'section_title' when mode='section'.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )

            flat = _parse_md_sections(all_lines)
            entry, error = _resolve_section(
                flat,
                section_path=section_path,
                section_title=section_title,
                section_level=section_level,
            )
            if error:
                return self._failure(
                    "NOT_FOUND",
                    error,
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )
            # Narrowed by _resolve_section contract: error is None → entry is not None
            assert entry is not None

            # Override line range to the section boundaries
            params["start_line"] = entry["line"]
            params["end_line"] = _section_end_line(flat, entry, total_lines)
            section_applied = True
            log.info(
                "read_file: section mode — %r → lines %d-%d",
                entry.get("path", entry.get("title")),
                entry["line"],
                params["end_line"],
            )

        # ── Normalize offset/limit to start_line/end_line ──────────────────
        tail_lines: int | None = params.get("tail_lines") or None
        max_lines = min(int(params.get("max_lines") or _DEFAULT_MAX_LINES), 1000)
        raw_offset = params.get("offset")
        if raw_offset is not None:
            if params.get("start_line") is not None or params.get("end_line") is not None:
                return self._failure(
                    "INVALID_PARAMS",
                    "'offset'/'limit' are mutually exclusive with 'start_line'/'end_line'.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )
            if tail_lines is not None:
                return self._failure(
                    "INVALID_PARAMS",
                    "'offset'/'limit' are incompatible with 'tail_lines'.",
                    execution_time_ms=int((time.monotonic() - t0) * 1000),
                )
            offset_val = int(raw_offset)
            limit_val = int(params.get("limit") or max_lines)
            effective_limit = min(limit_val, max_lines)
            params["start_line"] = offset_val + 1
            params["end_line"] = offset_val + effective_limit
            # Preserve original offset/limit for response metadata
            params["_offset"] = offset_val
            params["_limit"] = effective_limit

        # ── Tail mode or line range ───────────────────────────────────────
        if tail_lines is not None:
            # Tail mode: read last N lines (ignores start_line/end_line)
            if not section_applied and params.get("start_line") is not None:
                log.info(
                    "read_file: tail_lines=%d overrides start_line for %s",
                    tail_lines, filename,
                )
            tail_lines = max(1, int(tail_lines))
            selected = all_lines[-tail_lines:]
            start_line = max(1, total_lines - tail_lines + 1)
            end_line = total_lines
            tail_applied = True
        else:
            raw_start = params.get("start_line")
            raw_end = params.get("end_line")
            start_line = max(1, int(raw_start)) if raw_start is not None else 1
            end_line = (
                int(raw_end)
                if raw_end is not None
                else start_line + max_lines - 1
            )
            end_line = min(end_line, total_lines)
            selected = all_lines[start_line - 1 : end_line]
            tail_applied = False

        # ── Apply grep filter ─────────────────────────────────────────────
        grep_pattern: str | None = params.get("grep") or None
        grep_mode: str = params.get("grep_mode") or "substring"
        grep_case_sensitive: bool = params.get("grep_case_sensitive", False)
        grep_context: int = int(params.get("grep_context") or 0)
        grep_applied = False
        grep_matches = 0

        if grep_pattern:
            if grep_mode == "regex":
                try:
                    flags = 0 if grep_case_sensitive else re.IGNORECASE
                    regex = re.compile(grep_pattern, flags)
                except re.error as exc:
                    return self._failure(
                        "INVALID_PARAMS",
                        f"Invalid regex pattern {grep_pattern!r}: {exc}",
                        execution_time_ms=int((time.monotonic() - t0) * 1000),
                    )
                match_indices = [
                    i for i, ln in enumerate(selected)
                    if regex.search(ln)
                ]
            else:
                # Substring mode (preserves backward-compatible behavior)
                pattern = grep_pattern if grep_case_sensitive else grep_pattern.lower()
                match_indices = [
                    i for i, ln in enumerate(selected)
                    if (
                        pattern in ln
                        if grep_case_sensitive
                        else pattern in ln.lower()
                    )
                ]

            grep_matches = len(match_indices)

            if grep_context > 0 and match_indices:
                # Expand each match with context, merge overlapping blocks
                expanded: set[int] = set()
                for idx in match_indices:
                    lo = max(0, idx - grep_context)
                    hi = min(len(selected), idx + grep_context + 1)
                    expanded.update(range(lo, hi))
                selected = [selected[i] for i in sorted(expanded)]
            else:
                selected = [selected[i] for i in match_indices]

            grep_applied = True

        # ── Apply max_lines cap ───────────────────────────────────────────
        capped = len(selected) > max_lines
        selected = selected[:max_lines]

        content_out = "\n".join(selected)

        # ── Pagination metadata ───────────────────────────────────────────
        has_more = end_line < total_lines
        # Compute next_offset from original offset/limit if available,
        # otherwise derive from start_line
        _orig_offset = params.get("_offset")
        _orig_limit = params.get("_limit")
        if _orig_offset is not None and _orig_limit is not None:
            next_offset = _orig_offset + _orig_limit if has_more else None
        else:
            next_offset = end_line if has_more else None  # 0-based next

        # ── Revision token (opaque) ───────────────────────────────────────
        updated_at = result.get("updated_at")
        revision = updated_at.isoformat() if updated_at is not None else None

        # ── Build output ──────────────────────────────────────────────────
        out_data: dict = {
            **base_meta,
            "mode": mode,
            "encoding": encoding,
            "content": content_out,
            "total_lines": total_lines,
            "total_words": total_words,
            "total_chars": total_chars,
            "returned_lines": len(selected),
            "start_line": start_line,
            "end_line": end_line,
            "has_more": has_more,
            "next_offset": next_offset,
            "revision": revision,
            "tail_applied": tail_applied,
            "section_applied": section_applied,
            "grep_applied": grep_applied,
            "grep_matches": grep_matches,
            "grep_context": grep_context if grep_context > 0 else 0,
            "capped_at_max_lines": capped,
            "file_truncated_by_size": truncated_by_cap,
        }

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if capped or truncated_by_cap:
            # Build a helpful guidance message
            parts = [f"Returned {len(selected)} of {total_lines} lines"]
            if section_applied:
                parts.append("(section scoped)")
            elif tail_applied:
                parts.append(f"(tail={tail_lines})")
            else:
                parts.append(f"(start={start_line}, end={end_line})")
            if grep_applied:
                parts.append(f"grep={grep_pattern!r}")
                if grep_mode == "regex":
                    parts.append("(regex)")
            if capped:
                parts.append("— capped at max_lines")
            parts.append(
                ". Call again with different start_line/end_line to read more."
            )
            return self._partial(
                data=out_data,
                code="TRUNCATED",
                message=" ".join(parts),
                execution_time_ms=elapsed_ms,
            )

        return self._success(data=out_data, execution_time_ms=elapsed_ms)

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
