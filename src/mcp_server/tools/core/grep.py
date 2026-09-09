"""gSage AI — Grep tool.

Filters lines of a stored text file by a regex and saves the matching
lines as a **new** file — or, with ``invert`` (equivalent to GNU
``grep -v``), the lines that do **not** match. The source file is never
modified.

Regex flavor: **ERE-like** (Python ``re``), NOT BRE: groups are ``(...)``
and back-references are ``\\1`` — never ``\\(...\\)``.

Examples
--------
* ``pattern="ERROR|FATAL"``              — extract error lines from a log
* ``pattern="^\\s*#|^\\s*$", invert=true`` — strip comments and blank lines
* ``pattern="^2026-09-08"``              — keep only today's lines

Permission: ``core:grep``
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core._text_filter_shared import (
    TextAccessError,
    TextDecodeError,
    UnsupportedContentTypeError,
    access_error_code,
    build_file_error_partial,
    build_preview,
    detect_line_ending,
    join_lines,
    load_text_file,
    split_lines,
    store_text_output,
)
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────
_VALID_LINE_ENDINGS: frozenset[str] = frozenset({"auto", "lf", "crlf"})
_VALID_SCOPES: frozenset[str] = frozenset({"user", "department"})


# ── Filter worker ──────────────────────────────────────────────────────────


def _apply_grep(
    text: str,
    pattern: re.Pattern[str],
    *,
    invert: bool,
    ending: str,
) -> tuple[str, dict]:
    """Synchronous worker: keep matching (or non-matching) lines in order.

    Returns the filtered text and stats (``lines_in``, ``lines_out``,
    ``matches``).
    """
    lines = split_lines(text)
    kept: list[str] = []
    matches = 0
    for line in lines:
        is_match = pattern.search(line) is not None
        if is_match:
            matches += 1
        if is_match != invert:
            kept.append(line)

    result = join_lines(
        kept, ending, source_had_trailing_newline=text.endswith(("\n", "\r"))
    )
    stats = {
        "lines_in": len(lines),
        "lines_out": len(kept),
        "matches": matches,
        "invert": invert,
    }
    return result, stats


# ── Tool ───────────────────────────────────────────────────────────────────


class GrepTool(BaseTool):
    """Filter lines of a stored text file by regex and save the result as a
    new file.

    With ``invert=true`` the non-matching lines are returned instead
    (equivalent to GNU ``grep -v``). Regex is ERE-like, not BRE. The source
    file is never modified.
    """

    name: ClassVar[str] = "grep"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Filter lines of a stored text file by regex and save matching "
        "lines (or non-matching, with invert) as a new file."
    )
    category: ClassVar[str] = "file"
    permissions: ClassVar[list[str]] = ["core:grep"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    background_threshold_seconds: ClassVar[Optional[int]] = 50
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id", "pattern"],
        "additionalProperties": False,
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the source text file (GSageFile.id). "
                    "The source file is never modified."
                ),
            },
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "ERE-like regex (Python re, sed -E style), NOT BRE. "
                    "Lines matching it are kept — or, with invert=true, "
                    "kept are the lines that do NOT match (like grep -v)."
                ),
            },
            "invert": {
                "type": "boolean",
                "description": (
                    "When true, return the lines that do NOT match the "
                    "pattern (grep -v). Default false."
                ),
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "Optional output filename. Default: '<base>_grep.<ext>' "
                    "with numeric increment if taken."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "Optional MIME type of the output file. "
                    "Default: inferred from the output extension."
                ),
            },
            "encoding": {
                "type": "string",
                "description": (
                    "Input encoding. Default 'utf-8' (e.g. 'latin-1' for "
                    "legacy exports)."
                ),
            },
            "line_ending": {
                "type": "string",
                "enum": ["auto", "lf", "crlf"],
                "description": (
                    "Output line separator. Default 'auto': preserve the "
                    "source's dominant ending."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["user", "department"],
                "description": "Visibility of the generated file. Default 'user'.",
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "When true, apply the filter and return stats + preview "
                    "WITHOUT creating a file. Default false."
                ),
            },
        },
    }

    audit_field_mapping: ClassVar[dict] = {"target_entities": "file_id"}

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.monotonic()

        # ── Validate params ──────────────────────────────────────────────
        file_id = params.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_INPUT", "'file_id' is required.")

        pattern_str = params.get("pattern")
        if not isinstance(pattern_str, str) or not pattern_str:
            return self._failure(
                "INVALID_INPUT", "'pattern' is required and must be a non-empty string."
            )

        invert = bool(params.get("invert", False))

        encoding = params.get("encoding") or "utf-8"
        if not isinstance(encoding, str) or not encoding.strip():
            return self._failure("INVALID_INPUT", "'encoding' must be a string.")

        line_ending = params.get("line_ending") or "auto"
        if line_ending not in _VALID_LINE_ENDINGS:
            return self._failure("INVALID_INPUT", "'line_ending' must be one of: auto, lf, crlf.")

        scope = params.get("scope") or "user"
        if scope not in _VALID_SCOPES:
            return self._failure("INVALID_INPUT", "'scope' must be one of: user, department.")

        dry_run = bool(params.get("dry_run", False))

        output_filename = params.get("output_filename")
        if output_filename is not None and (
            not isinstance(output_filename, str) or not output_filename.strip()
        ):
            return self._failure("INVALID_INPUT", "'output_filename' must be a non-empty string.")

        content_type = params.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            return self._failure("INVALID_INPUT", "'content_type' must be a string.")

        # ── Compile the pattern first (fail fast) ────────────────────────
        try:
            pattern = re.compile(pattern_str)
        except re.error as exc:
            return self._failure(
                "REGEX_INVALID",
                f"'pattern' {pattern_str!r} is not a valid regex: {exc}",
            )

        # ── Load source file ─────────────────────────────────────────────
        try:
            text, file_meta = await load_text_file(
                self, agent_context, file_id, encoding=encoding
            )
        except TextAccessError as exc:
            return await build_file_error_partial(
                self, agent_context, code=access_error_code(exc), message=str(exc)
            )
        except UnsupportedContentTypeError as exc:
            return self._failure("UNSUPPORTED_CONTENT_TYPE", str(exc))
        except TextDecodeError as exc:
            return self._failure("DECODE_ERROR", str(exc))
        except Exception as exc:  # pragma: no cover
            logger.exception("grep: unexpected load failure: %s", exc)
            return self._failure(
                "INTERNAL_ERROR", f"Failed to load file: {exc}", retryable=True
            )

        # ── Apply filter (CPU-bound — offload to a worker thread) ────────
        if line_ending == "lf":
            ending = "\n"
        elif line_ending == "crlf":
            ending = "\r\n"
        else:
            ending = detect_line_ending(text)

        try:
            result_text, stats = await asyncio.to_thread(
                _apply_grep, text, pattern, invert=invert, ending=ending
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("grep: filter failed: %s", exc)
            return self._failure(
                "INTERNAL_ERROR", f"Failed to apply filter: {exc}", retryable=True
            )

        stats["dry_run"] = dry_run
        preview, truncated = build_preview(result_text)

        agent_hint = (
            "Use read_file with file_id to inspect the full result, or pass "
            "it to csv_describe/csv_query for tabular analysis."
        )

        # ── dry_run: stats + preview, no file ────────────────────────────
        if dry_run:
            return self._success(
                data={
                    "source_file": {
                        "file_id": file_meta["file_id"],
                        "filename": file_meta["filename"],
                    },
                    "stats": stats,
                    "preview": preview,
                    "preview_truncated": truncated,
                    "agent_hint": agent_hint,
                },
                execution_time_ms=int((time.monotonic() - start) * 1000),
            )

        # ── Persist output as a new file ─────────────────────────────────
        output_file = await store_text_output(
            self,
            agent_context,
            text=result_text,
            source_filename=file_meta["filename"],
            marker="grep",
            output_filename=output_filename,
            content_type=content_type,
            scope=scope,
            description=(
                f"grep result from '{file_meta['filename']}' "
                f"({stats['lines_out']} of {stats['lines_in']} lines, "
                f"invert={invert})"
            ),
        )
        if output_file is None:
            return self._failure(
                "STORE_FAILED",
                "Output could not be saved to storage (MinIO/DB failure). "
                "Check MCP server logs.",
                retryable=True,
            )

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "file_id": output_file.get("file_id"),
                "filename": output_file.get("filename"),
                "content_type": output_file.get("content_type"),
                "size_bytes": output_file.get("size_bytes"),
                "download_path": output_file.get("download_path"),
                "source_file": {
                    "file_id": file_meta["file_id"],
                    "filename": file_meta["filename"],
                },
                "stats": stats,
                "preview": preview,
                "preview_truncated": truncated,
                "agent_hint": agent_hint,
            },
            execution_time_ms=elapsed,
        )
