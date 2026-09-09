"""gSage AI — Sed tool.

Applies ordered sed-like substitution expressions to a stored text file
(``.txt``, ``.csv``, ``.log``, ``.html``, ``.json``, …) and saves the
result as a **new** file. The source file is never modified.

Regex flavor: **ERE-like** (Python ``re``, GNU ``sed -E`` style), NOT BRE:
groups are ``(...)`` and back-references are ``\\1`` — never ``\\(...\\)``.

Supported dialect (v1)
----------------------
Only the ``s`` command, with optional address prefix:

* ``s/pattern/replacement/flags``         — substitute (flags: ``g``, ``i``)
* ``[address]s/pattern/replacement/flags``— restrict to matching lines

Addresses: ``N``, ``$``, ``N,M``, ``/regex/``, ``/regex/,/regex/``,
``N,/regex/``. Any single non-alphanumeric character can be used as
delimiter (``s|a|b|g``). Other sed commands (``d``, ``e``, ``w``, ``r``,
``a``, ``i``, ``c``, ``y``, ``p``, ``q``, …) are rejected with
``SED_BLOCKED_COMMAND``.

Replacement escapes follow GNU sed semantics: ``\\/`` writes a literal
delimiter, ``\\\\`` a backslash, ``\\&`` a literal ``&``, ``\\1``–``\\9`` are
group back-references, and any other ``\\x`` collapses to ``x``. A bare
``/`` in the replacement must be escaped as ``\\/`` — or use an alternate
delimiter (``s#…#…#``). Note: a bare ``&`` is literal (unlike GNU sed,
where it means the whole match).

Examples
--------
* ``s/^\\[[0-9 :-]+\\] //``    — strip a log timestamp prefix
* ``s/  +/,/g``                — collapse runs of spaces into CSV commas
* ``s/;$//``                   — remove a trailing semicolon
* ``2,5s/foo/bar/g``           — substitute only on lines 2–5
* ``s/(ERROR|WARN)/!!\\1!!/i`` — alternation + back-reference + ``i`` flag

Permission: ``core:sed``
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
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
_MAX_EXPRESSIONS: int = 20

# sed command letters that exist in GNU sed but are rejected in v1.
_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    "dewrqaicyp=nNhHgGxbTt:"
)

_VALID_LINE_ENDINGS: frozenset[str] = frozenset({"auto", "lf", "crlf"})
_VALID_SCOPES: frozenset[str] = frozenset({"user", "department"})


# ── sed expression engine (restricted dialect) ─────────────────────────────


class SedSyntaxError(ValueError):
    """Malformed sed expression (→ ``SED_PARSE_ERROR``)."""


class SedBlockedCommand(ValueError):
    """Rejected sed command (→ ``SED_BLOCKED_COMMAND``)."""


@dataclass
class _Addr:
    """A parsed sed address."""

    kind: str  # "line" | "last" | "regex"
    line: Optional[int] = None
    regex: Optional[re.Pattern[str]] = None

    def matches(self, idx: int, line: str, total: int) -> bool:
        if self.kind == "line":
            return idx == self.line
        if self.kind == "last":
            return idx == total
        assert self.regex is not None
        return bool(self.regex.search(line))


@dataclass
class _SubExpr:
    """A compiled substitution expression."""

    addr1: Optional[_Addr]
    addr2: Optional[_Addr]
    pattern: re.Pattern[str]
    replacement: str
    count: int  # 0 = all occurrences ("g"), 1 = first only
    raw: str


def _parse_address(text: str, pos: int) -> tuple[Optional[_Addr], int]:
    """Parse an address at *pos*. Returns ``(None, pos)`` when none applies."""
    if pos >= len(text):
        return None, pos
    ch = text[pos]
    if ch.isdigit():
        m = re.match(r"\d+", text[pos:])
        assert m is not None
        return _Addr(kind="line", line=int(m.group())), pos + len(m.group())
    if ch == "$":
        return _Addr(kind="last"), pos + 1
    if ch == "/":
        i = pos + 1
        buf: list[str] = []
        while i < len(text):
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                # Keep escapes intact (\/ is a literal '/', \[ a literal '[').
                buf.append("\\")
                buf.append(text[i + 1])
                i += 2
                continue
            if c == "/":
                i += 1
                break
            buf.append(c)
            i += 1
        else:
            raise SedSyntaxError("unterminated regex address (missing closing '/')")
        pattern_str = "".join(buf)
        try:
            compiled = re.compile(pattern_str)
        except re.error as exc:
            raise SedSyntaxError(f"invalid address regex: {exc}") from exc
        return _Addr(kind="regex", regex=compiled), i
    return None, pos


def _parse_substitution(text: str, pos: int) -> tuple[re.Pattern[str], str, int]:
    """Parse the ``s<delim>pattern<delim>replacement<delim>flags`` command.

    *text[pos]* must be ``"s"``. Returns ``(compiled_pattern, replacement,
    count)``.
    """
    if pos + 1 >= len(text):
        raise SedSyntaxError("missing delimiter after 's'")
    delim = text[pos + 1]
    if delim.isalnum() or delim.isspace() or delim == "\\":
        raise SedSyntaxError(f"invalid delimiter {delim!r} after 's'")

    # ── Pattern (up to the next unescaped delimiter) ─────────────────────
    i = pos + 2
    pat_buf: list[str] = []
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            # Keep escapes intact: '\/' is a literal '/', '\[' a literal '[',
            # and '\1' a back-reference.
            pat_buf.append("\\")
            pat_buf.append(text[i + 1])
            i += 2
            continue
        if c == delim:
            i += 1
            break
        pat_buf.append(c)
        i += 1
    else:
        raise SedSyntaxError("unterminated pattern (missing closing delimiter)")

    # ── Replacement (escapes normalized to GNU sed semantics) ───────────
    # \/ (escaped delimiter) → the delimiter; \\ → one backslash;
    # \& → literal &; \1-\9 kept as back-references; any other \x
    # collapses to x (avoids Python re.sub "bad escape" errors).
    repl_buf: list[str] = []
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == delim:
                repl_buf.append(delim)
            elif nxt == "\\":
                repl_buf.append("\\")
                repl_buf.append("\\")
            elif nxt == "&":
                repl_buf.append("&")
            elif nxt.isdigit():
                repl_buf.append("\\")
                repl_buf.append(nxt)
            else:
                repl_buf.append(nxt)
            i += 2
            continue
        if c == delim:
            i += 1
            break
        repl_buf.append(c)
        i += 1
    else:
        raise SedSyntaxError("unterminated replacement (missing closing delimiter)")

    # ── Flags ────────────────────────────────────────────────────────────
    flags = text[i:]
    flag_set = set(flags)
    unknown = flag_set - {"g", "i"}
    if unknown:
        raise SedSyntaxError(f"unsupported flag(s): {''.join(sorted(unknown))}")
    if len(flags) != len(flag_set):
        raise SedSyntaxError(f"duplicate flag in {flags!r}")

    pattern_str = "".join(pat_buf)
    try:
        compiled = re.compile(pattern_str, re.IGNORECASE if "i" in flag_set else 0)
    except re.error as exc:
        raise SedSyntaxError(f"invalid regex: {exc}") from exc

    count = 0 if "g" in flag_set else 1
    return compiled, "".join(repl_buf), count


def parse_expression(expr: str) -> _SubExpr:
    """Parse and compile one sed expression (v1 dialect).

    Raises
    ------
    SedSyntaxError
        Malformed expression.
    SedBlockedCommand
        The expression uses a sed command outside the supported subset.
    """
    text = expr.strip()
    if not text:
        raise SedSyntaxError("empty expression")

    pos = 0
    addr1, pos = _parse_address(text, pos)
    addr2: Optional[_Addr] = None
    if addr1 is not None:
        if pos < len(text) and text[pos] == ",":
            pos += 1
            addr2, pos = _parse_address(text, pos)
            if addr2 is None:
                raise SedSyntaxError("expected a second address after ','")
        while pos < len(text) and text[pos] in " \t":
            pos += 1

    if pos >= len(text):
        raise SedSyntaxError("missing command after address")

    cmd = text[pos]
    if cmd != "s":
        if cmd in _BLOCKED_COMMANDS:
            raise SedBlockedCommand(
                f"command {cmd!r} is not supported in v1. "
                "Only the 's' substitution command is available "
                "(e.g. 's/pattern/replacement/g')."
            )
        raise SedSyntaxError(f"unknown command {cmd!r}")

    pattern, replacement, count = _parse_substitution(text, pos)
    return _SubExpr(
        addr1=addr1,
        addr2=addr2,
        pattern=pattern,
        replacement=replacement,
        count=count,
        raw=expr,
    )


def apply_expressions(
    lines: list[str],
    compiled: list[_SubExpr],
) -> tuple[list[str], dict]:
    """Apply compiled expressions sequentially over logical *lines*.

    Returns the transformed lines and stats (``substitutions`` count).
    """
    stats: dict = {"substitutions": 0}
    out = list(lines)
    total = len(out)

    for expr in compiled:
        next_out: list[str] = []
        active = False
        for idx, line in enumerate(out, start=1):
            if expr.addr1 is None:
                apply = True
            else:
                if not active and expr.addr1.matches(idx, line, total):
                    active = True
                apply = active
                # A two-address range is inclusive of both ends.
                if (
                    active
                    and expr.addr2 is not None
                    and expr.addr2.matches(idx, line, total)
                ):
                    active = False

            if apply:
                new_line, n = expr.pattern.subn(
                    expr.replacement, line, count=expr.count
                )
                stats["substitutions"] += n
                next_out.append(new_line)
            else:
                next_out.append(line)
        out = next_out

    return out, stats


def _apply_sed(text: str, compiled: list[_SubExpr], ending: str) -> tuple[str, dict]:
    """Synchronous worker: split → transform → join (runs in a thread)."""
    lines = split_lines(text)
    out, stats = apply_expressions(lines, compiled)
    result = join_lines(
        out, ending, source_had_trailing_newline=text.endswith(("\n", "\r"))
    )
    stats["lines_in"] = len(lines)
    stats["lines_out"] = len(out)
    return result, stats


# ── Tool ───────────────────────────────────────────────────────────────────


class SedTool(BaseTool):
    """Apply sed-like substitution expressions to a stored text file and
    save the result as a new file.

    Only the ``s`` command is supported (see module docstring for the
    dialect and examples). Regex is ERE-like, not BRE. The source file is
    never modified.
    """

    name: ClassVar[str] = "sed"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Apply sed-like expressions to a stored text file and save the "
        "result as a new file."
    )
    category: ClassVar[str] = "file"
    permissions: ClassVar[list[str]] = ["core:sed"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    background_threshold_seconds: ClassVar[Optional[int]] = 50
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id", "expressions"],
        "additionalProperties": False,
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the source text file (GSageFile.id). "
                    "The source file is never modified."
                ),
            },
            "expressions": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_EXPRESSIONS,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Ordered sed expressions applied sequentially. Only the "
                    "'s' command is supported: "
                    "'[address]s/pattern/replacement/flags' (flags: g, i; "
                    "addresses: N, $, N,M, /regex/, ranges). Other commands "
                    "are rejected. Regex is ERE-like (sed -E style), NOT BRE: "
                    "groups use (...) and back-references \\1. In the "
                    "replacement, escape the delimiter (e.g. \\/ when using "
                    "'/' as delimiter), use \\1-\\9 for groups and \\\\ for a "
                    "literal backslash."
                ),
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "Optional output filename (e.g. 'access.csv'). "
                    "Default: '<base>_sed.<ext>' with numeric increment if taken."
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
                    "When true, apply expressions and return stats + preview "
                    "WITHOUT creating a file. Use to validate expressions "
                    "before committing. Default false."
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

        expressions = params.get("expressions")
        if (
            not isinstance(expressions, list)
            or not expressions
            or len(expressions) > _MAX_EXPRESSIONS
            or not all(isinstance(e, str) and e.strip() for e in expressions)
        ):
            return self._failure(
                "INVALID_INPUT",
                f"'expressions' must be a list of 1–{_MAX_EXPRESSIONS} "
                "non-empty strings.",
            )

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

        # ── Parse expressions first (fail fast, before loading the file) ─
        compiled: list[_SubExpr] = []
        for index, expr in enumerate(expressions, start=1):
            try:
                compiled.append(parse_expression(expr))
            except SedBlockedCommand as exc:
                return self._failure(
                    "SED_BLOCKED_COMMAND",
                    f"Expression #{index} {expr!r}: {exc}",
                )
            except SedSyntaxError as exc:
                return self._failure(
                    "SED_PARSE_ERROR",
                    f"Expression #{index} {expr!r}: {exc}",
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
            logger.exception("sed: unexpected load failure: %s", exc)
            return self._failure(
                "INTERNAL_ERROR", f"Failed to load file: {exc}", retryable=True
            )

        # ── Apply expressions (CPU-bound — offload to a worker thread) ───
        if line_ending == "lf":
            ending = "\n"
        elif line_ending == "crlf":
            ending = "\r\n"
        else:
            ending = detect_line_ending(text)

        try:
            result_text, stats = await asyncio.to_thread(
                _apply_sed, text, compiled, ending
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("sed: transform failed: %s", exc)
            return self._failure(
                "INTERNAL_ERROR", f"Failed to apply expressions: {exc}", retryable=True
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
            marker="sed",
            output_filename=output_filename,
            content_type=content_type,
            scope=scope,
            description=(
                f"sed result from '{file_meta['filename']}' "
                f"({stats['lines_in']} lines, {stats['substitutions']} substitutions)"
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
