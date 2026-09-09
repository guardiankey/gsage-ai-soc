"""Shared pipeline helpers for the core text-filter tools (``sed`` / ``grep``).

Both tools follow the same file-in/file-out flow:

    file_id -> _load_file_with_reason (access + org scoping)
             -> text/content-type checks + size cap
             -> decode (encoding)
             -> transform (sed expressions / grep regex filter)
             -> optional dry_run (no file created)
             -> _store_file -> new GSageFile

This module factors out the parts shared by the two tools:

* ``load_text_file``        — load bytes via ``_load_file_with_reason``, map
  access failures to the ``FILE_*`` error codes used by the CSV tools,
  enforce the text-only and size-cap rules, and decode to ``str``.
* ``list_available_files``  — list the files available in the current
  session context (returned when the requested file is missing or
  inaccessible).
* ``compute_output_filename`` / ``store_text_output`` — derive
  ``<base>_sed.<ext>`` / ``<base>_grep.<ext>`` output names with numeric
  increment, and persist the result through ``_store_file``.
* Small helpers: ``detect_line_ending``, ``build_preview``, filename
  sanitisation, and the partial-result builder for file-access errors.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import TYPE_CHECKING, Optional

from src.mcp_server.tools.core._file_shared import infer_content_type, is_text_content

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.mcp_server.tools.base import BaseTool, ToolResult
    from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Tunables ───────────────────────────────────────────────────────────────
MAX_INPUT_BYTES: int = 1024 * 1024 * 1024  # 1 GB (same default as the CSV loader)
PREVIEW_MAX_LINES: int = 100
PREVIEW_MAX_CHARS: int = 4000
AVAILABLE_FILES_LIMIT: int = 50

# Matches stems that already carry a `_sed` / `_grep` / `_sed2` … suffix.
# Group 1 = base name (before the marker), used for idempotent naming.
_MARKER_RE: re.Pattern[str] = re.compile(
    r"^(?P<base>.+?)_(?:sed|grep)\d*$", re.IGNORECASE
)

# ── Access errors ──────────────────────────────────────────────────────────

# Failure reasons from ``BaseTool._load_file_with_reason`` mapped to the
# public ``error_code`` values surfaced to the LLM (same mapping as the CSV
# tools — see ``csv_loader.ACCESS_REASON_TO_ERROR_CODE``).
ACCESS_REASON_TO_ERROR_CODE: dict[str, str] = {
    "NOT_FOUND": "FILE_NOT_FOUND",
    "PURGED": "FILE_EXPIRED",
    "ACCESS_DENIED": "FILE_ACCESS_DENIED",
    "LOAD_FAILED": "FILE_LOAD_FAILED",
    "TOO_LARGE": "FILE_TOO_LARGE",
}


class TextAccessError(FileNotFoundError):
    """Raised when a text file cannot be loaded (access / size failures).

    The ``reason`` attribute carries one of the codes returned by
    :meth:`BaseTool._load_file_with_reason` (``"NOT_FOUND"``, ``"PURGED"``,
    ``"ACCESS_DENIED"``, ``"LOAD_FAILED"``, ``"TOO_LARGE"``).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnsupportedContentTypeError(ValueError):
    """Raised when the source file is not a supported text format."""


class TextDecodeError(ValueError):
    """Raised when the source file cannot be decoded with the given encoding."""


def access_error_code(exc: FileNotFoundError) -> str:
    """Return the canonical ``error_code`` for a text-load failure."""
    reason = getattr(exc, "reason", None)
    return ACCESS_REASON_TO_ERROR_CODE.get(reason or "", "FILE_NOT_FOUND")


_FILE_ERROR_MESSAGES: dict[str, str] = {
    "NOT_FOUND": (
        "File '{file_id}' does not exist in org '{org_id}'. "
        "It may have been deleted, or the ID is wrong."
    ),
    "PURGED": (
        "File '{file_id}' has expired and its contents were purged. "
        "Attachments have a TTL — ask the user to upload it again."
    ),
    "ACCESS_DENIED": (
        "Access denied to file '{file_id}'. "
        "The current user does not own this file and it is not org-scoped."
    ),
    "LOAD_FAILED": (
        "Failed to read file '{file_id}' from storage (transient error)."
    ),
}


# ── Loading ────────────────────────────────────────────────────────────────


async def load_text_file(
    tool: "BaseTool",
    agent_context: "AgentContext",
    file_id: str,
    *,
    encoding: str = "utf-8",
    max_bytes: int = MAX_INPUT_BYTES,
) -> tuple[str, dict]:
    """Load a stored text file via ``_load_file_with_reason`` and decode it.

    Raises
    ------
    TextAccessError
        File not found, expired, inaccessible, or too large.
    UnsupportedContentTypeError
        The file is not a supported text format.
    TextDecodeError
        The bytes could not be decoded with *encoding*.
    """
    org_id = str(agent_context.org_id)

    file_meta, reason = await tool._load_file_with_reason(
        file_id=file_id,
        org_id=org_id,
        user_id=str(agent_context.user_id),
        dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
        max_bytes=max_bytes,
    )
    if file_meta is None:
        msg = _FILE_ERROR_MESSAGES.get(reason or "", "File '{file_id}' unavailable.")
        raise TextAccessError(reason or "NOT_FOUND", msg.format(file_id=file_id, org_id=org_id))

    # Truncation means the file exceeded max_bytes — never transform a
    # partial view of the file.
    if file_meta.get("truncated"):
        size_mb = file_meta.get("size_bytes", 0) / (1024 * 1024)
        limit_mb = max_bytes / (1024 * 1024)
        raise TextAccessError(
            "TOO_LARGE",
            f"File '{file_id}' is too large to process "
            f"({size_mb:.0f} MB; limit is {limit_mb:.0f} MB). "
            "Ask the user to split the file into smaller chunks.",
        )

    filename: str = file_meta.get("filename") or "unknown"
    content_type: str = file_meta.get("content_type") or ""
    if not is_text_content(content_type):
        raise UnsupportedContentTypeError(
            f"File {filename!r} has unsupported content type {content_type!r}. "
            "Only text-based formats are supported (txt, csv, log, html, json, "
            "xml, yaml, md, …)."
        )

    raw: bytes = file_meta.get("data") or b""
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise TextDecodeError(
            f"File {filename!r} could not be decoded as {encoding!r}. "
            "Set the 'encoding' parameter (e.g. 'latin-1') to match the file."
        ) from exc

    meta = {
        "file_id": str(file_meta.get("file_id", file_id)),
        "filename": filename,
        "content_type": content_type,
        "size_bytes": int(file_meta.get("size_bytes", len(raw))),
    }
    return text, meta


# ── Available files listing ────────────────────────────────────────────────


async def list_available_files(
    agent_context: "AgentContext",
    *,
    limit: int = AVAILABLE_FILES_LIMIT,
) -> list[dict]:
    """Return the text files available in the current session context.

    Reuses the same query as ``read_file._list_attachments``: ``GSageFile``
    rows scoped to the current org / session (fallback: user), non-purged,
    ordered by ``created_at DESC``.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from src.mcp_server.tenant_context import get_tenant_headers_or_none  # noqa: PLC0415
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.generated_file import GSageFile  # noqa: PLC0415

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

        stmt = stmt.order_by(GSageFile.created_at.desc()).limit(limit)
        result = await db.execute(stmt)
        rows = result.scalars().all()

    return [
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


async def build_file_error_partial(
    tool: "BaseTool",
    agent_context: "AgentContext",
    *,
    code: str,
    message: str,
) -> "ToolResult":
    """Build the partial result returned when the source file is unavailable.

    Carries the list of files available in the session context so the LLM
    can recover without extra round-trips (spec §2.3).
    """
    try:
        available = await list_available_files(agent_context)
    except Exception:  # pragma: no cover - listing is best-effort
        logger.exception("text_filter: could not list available files")
        available = []

    hint = (
        "The requested file could not be read. Pick one of the "
        "available_files below, or list attachments with read_file."
    )
    return tool._partial(
        data={"available_files": available, "count": len(available), "hint": hint},
        code=code,
        message=message,
        retryable=False,
    )


# ── Text helpers ───────────────────────────────────────────────────────────


def detect_line_ending(text: str) -> str:
    """Return the dominant line separator (``"\\n"`` or ``"\\r\\n"``)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf > 0 and crlf >= lf:
        return "\r\n"
    return "\n"


def split_lines(text: str) -> list[str]:
    """Split into logical lines (trailing newline terminates, not creates)."""
    return text.splitlines()


def join_lines(lines: list[str], ending: str, *, source_had_trailing_newline: bool) -> str:
    """Join logical lines with *ending*, restoring the trailing newline."""
    result = ending.join(lines)
    if source_had_trailing_newline and lines:
        result += ending
    return result


def build_preview(
    text: str,
    *,
    max_lines: int = PREVIEW_MAX_LINES,
    max_chars: int = PREVIEW_MAX_CHARS,
) -> tuple[str, bool]:
    """Return a truncated preview of *text* plus a ``truncated`` flag."""
    lines = text.splitlines()
    preview_lines = lines[:max_lines]
    preview = "\n".join(preview_lines)
    truncated = len(lines) > len(preview_lines) or len(preview) > max_chars
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "…"
    return preview, truncated


def sanitize_filename(name: str, *, max_len: int = 200) -> str:
    """Sanitise a user-provided output filename (no path separators)."""
    cleaned = "".join(
        c if c.isalnum() or c in "._- " else "_" for c in name.strip()
    ).strip() or "output.txt"
    return cleaned[:max_len]


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards in *value* for use in an ``ilike`` pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── Output naming and storage ──────────────────────────────────────────────


def compute_output_filename(
    filename: str,
    marker: str,
    *,
    output_filename: Optional[str] = None,
    existing_filenames: Optional[set[str]] = None,
) -> str:
    """Derive the output filename for a text-filter run.

    Default naming (spec §5):

    * ``access.log`` → ``access_sed.log`` (``marker="sed"``)
    * name taken → ``access_sed2.log``, ``access_sed3.log``, …
    * an explicit ``output_filename`` wins over the derived name.

    A source that already carries a ``_sed*`` / ``_grep*`` suffix has its
    base restored first, so repeated runs never grow the name endlessly.
    """
    if output_filename:
        return sanitize_filename(output_filename)

    stem, ext = os.path.splitext(filename)
    m = _MARKER_RE.match(stem)
    base = m.group("base") if m else stem

    existing_lower = {f.lower() for f in (existing_filenames or set())}

    candidate = f"{base}_{marker}{ext}"
    if candidate.lower() not in existing_lower:
        return candidate
    i = 2
    while True:
        candidate = f"{base}_{marker}{i}{ext}"
        if candidate.lower() not in existing_lower:
            return candidate
        i += 1


async def store_text_output(
    tool: "BaseTool",
    agent_context: "AgentContext",
    *,
    text: str,
    source_filename: str,
    marker: str,
    output_filename: Optional[str] = None,
    content_type: Optional[str] = None,
    scope: str = "user",
    description: Optional[str] = None,
) -> Optional[dict]:
    """Pick the next free output name and persist *text* via ``_store_file``.

    Acquires a PostgreSQL advisory transaction lock keyed on
    ``(org_id, base, marker)`` before querying existing filenames,
    preventing concurrent requests from allocating the same suffix
    (same pattern as ``csv_shared._fetch_edited_filenames``).

    Returns the stored-file dict from ``_store_file``, or ``None`` on
    storage failure (MinIO unavailable, size exceeded, …).
    """
    from sqlalchemy import select, text as sql_text  # noqa: PLC0415

    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.generated_file import GSageFile  # noqa: PLC0415

    stem, ext = os.path.splitext(source_filename)
    m = _MARKER_RE.match(stem)
    base = m.group("base") if m else stem

    lock_bytes = hashlib.md5(f"{agent_context.org_id}:{base}_{marker}".encode()).digest()
    lock_int = int.from_bytes(lock_bytes[:8], "big", signed=True)

    async with _get_session_maker()() as db:
        await db.execute(
            sql_text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=lock_int)
        )

        stmt = (
            select(GSageFile.filename)
            .where(
                GSageFile.org_id == agent_context.org_id,
                GSageFile.user_id == agent_context.user_id,
                GSageFile.filename.ilike(f"{_escape_like(base)}_{marker}%"),
                GSageFile.purged_at.is_(None),
            )
        )
        result = await db.execute(stmt)
        existing = {row[0] for row in result.all()}

        final_name = compute_output_filename(
            source_filename,
            marker,
            output_filename=output_filename,
            existing_filenames=existing,
        )
        final_content_type = content_type or infer_content_type(final_name)

        return await tool._store_file(
            data=text.encode("utf-8"),
            filename=final_name,
            content_type=final_content_type,
            agent_context=agent_context,
            session=db,
            description=description,
            scope=scope,
        )
