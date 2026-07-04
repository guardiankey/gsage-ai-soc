"""gSage AI — Convert to Markdown tool.

Converts an attached document (PDF, DOCX, PPTX, XLSX, HTML, RTF) to Markdown
and saves the result as a downloadable artifact.  Plain-text attachments
are passed through unchanged.

Permission: ``core:convert_md``.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
_MAX_FILE_BYTES = 200 * 1024 * 1024  # 200 MB
_MAX_MD_CHARS = 5 * 1024 * 1024  # 5 MB generated Markdown
_PREVIEW_CHARS = 200  # Fixed inline preview size

# Content-type → internal format label
_CONTENT_TYPE_MAP: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/html": "html",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "text/richtext": "rtf",
}

# Content-type prefixes treated as plain-text passthrough
_TEXT_PREFIXES: tuple[str, ...] = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/x-sh",
    "application/csv",
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _detect_format(content_type: str) -> str | None:
    """Map a MIME content-type to an internal format label.

    Returns ``"text"`` for plain-text types, a known format key, or
    ``None`` when the type is unsupported.
    """
    ct = content_type.lower().split(";")[0].strip()
    if ct in _CONTENT_TYPE_MAP:
        return _CONTENT_TYPE_MAP[ct]
    if any(ct.startswith(p) for p in _TEXT_PREFIXES):
        return "text"
    return None


def _convert_with_markitdown(data: bytes) -> str:
    """Convert *data* to Markdown via ``markitdown``.

    Returns the Markdown text.  Raises on conversion failure.
    """
    import io

    from markitdown import MarkItDown  # noqa: PLC0415

    md = MarkItDown()
    result = md.convert(io.BytesIO(data))
    return result.text_content


def _convert_rtf(data: bytes) -> str:
    """Convert RTF *data* to plain text via ``striprtf``.

    Returns the extracted text.  Raises on conversion failure.
    """
    from striprtf.striprtf import rtf_to_text  # noqa: PLC0415

    return rtf_to_text(data.decode("utf-8", errors="replace"))


# ── Tool ─────────────────────────────────────────────────────────────────────


class ConvertToMdTool(BaseTool):
    """Convert a document attachment (PDF, DOCX, PPTX, XLSX, HTML, RTF) to Markdown.

    Provide the ``file_id`` of an attachment already uploaded to the chat.
    The tool detects the format automatically, converts the content to
    Markdown, saves it as a downloadable artifact, and returns a short
    inline preview (200 characters).

    Plain-text files (TXT, CSV, JSON, XML, …) are passed through unchanged.
    """

    name: ClassVar[str] = "convert_to_md"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Convert an attached document (PDF, DOCX, PPTX, XLSX, HTML, RTF, TXT) "
        "to Markdown. Saves the result as a downloadable artifact."
    )
    category: ClassVar[str] = "file"
    permissions: ClassVar[list[str]] = ["core:convert_md"]
    rate_limit_per_minute: ClassVar[int] = 15
    timeout_seconds: ClassVar[int] = 90
    requires_approval: ClassVar[bool] = False
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id"],
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the attached file to convert. "
                    "Use list_recent_artifacts to discover available files."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Helpers ───────────────────────────────────────────────────────

    async def _store_or_fallback(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        agent_context: AgentContext,
        description: str,
    ) -> tuple[Optional[dict], Optional[str]]:
        """Store *data* as a file artifact, returning error instead of raising.

        Long-running PDF conversions can exceed the async DB session
        lifetime.  When storage fails we return the error message so
        the caller can serve the Markdown inline instead.
        """
        from src.mcp_server.tools.base import _tool_session_ctx

        session = _tool_session_ctx.get()
        if session is None:
            return None, "no DB session available"

        try:
            artifact = await self._store_file(
                data=data,
                filename=filename,
                content_type=content_type,
                agent_context=agent_context,
                session=session,
                description=description,
            )
            return artifact, None
        except Exception as exc:
            return None, str(exc)

    # ── Execute ───────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()

        # ── Validate file_id ─────────────────────────────────────────────
        file_id = params.get("file_id", "")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_PARAMS", "'file_id' is required.")
        try:
            uuid.UUID(file_id)
        except ValueError:
            return self._failure(
                "INVALID_PARAMS",
                f"'file_id' is not a valid UUID: {file_id!r}",
            )

        # ── Load file ────────────────────────────────────────────────────
        file_meta = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_MAX_FILE_BYTES,
        )
        if file_meta is None:
            return self._failure(
                "NOT_FOUND",
                f"File {file_id!r} not found or you do not have access to it.",
            )

        filename: str = file_meta.get("filename", "unknown")
        content_type: str = file_meta.get("content_type", "application/octet-stream")
        data: bytes = file_meta.get("data", b"")
        file_size: int = file_meta.get("size_bytes", len(data))

        if not data:
            return self._failure(
                "EMPTY_FILE", f"File {filename!r} is empty."
            )

        # ── Detect format ────────────────────────────────────────────────
        source_format = _detect_format(content_type)
        if source_format is None:
            supported = sorted(
                set(_CONTENT_TYPE_MAP.values())
                | {"text (" + ", ".join(_TEXT_PREFIXES) + ")"}
            )
            return self._failure(
                "UNSUPPORTED_FORMAT",
                f"File {filename!r} has unsupported content type "
                f"{content_type!r}. Supported formats: {', '.join(supported)}.",
            )

        # ── Convert ──────────────────────────────────────────────────────
        try:
            if source_format == "text":
                md_content = data.decode("utf-8", errors="replace")
                log.info(
                    "convert_to_md: %s is plain text (%d bytes), passthrough",
                    filename, file_size,
                )
            elif source_format == "rtf":
                md_content = _convert_rtf(data)
                log.info(
                    "convert_to_md: %s (rtf) converted to text (%d chars)",
                    filename, len(md_content),
                )
            else:
                md_content = _convert_with_markitdown(data)
                log.info(
                    "convert_to_md: %s (%s) converted to Markdown (%d chars)",
                    filename, source_format, len(md_content),
                )
        except Exception as exc:
            log.warning(
                "convert_to_md: conversion failed for %s (%s): %s",
                filename, source_format, exc,
            )
            return self._failure(
                "CONVERSION_ERROR",
                f"Failed to convert {filename!r}: {exc}. "
                "The file may be corrupted, encrypted, or password-protected.",
            )

        if not md_content or not md_content.strip():
            return self._failure(
                "CONVERSION_ERROR",
                f"Conversion of {filename!r} produced empty output. "
                "The file may contain only images or non-extractable content.",
            )

        # ── Truncate oversized Markdown ──────────────────────────────────
        md_truncated = len(md_content) > _MAX_MD_CHARS
        if md_truncated:
            log.warning(
                "convert_to_md: Markdown for %s exceeds %d chars, truncating",
                filename, _MAX_MD_CHARS,
            )
            md_content = md_content[:_MAX_MD_CHARS]

        md_chars = len(md_content)

        # ── Save artifact ────────────────────────────────────────────────
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(
            c if c.isalnum() or c in "._- " else "_" for c in filename
        )[:80]
        artifact_filename = f"convert_{ts}_{safe_name}.md"

        artifact, store_error = await self._store_or_fallback(
            data=md_content.encode("utf-8"),
            filename=artifact_filename,
            content_type="text/markdown",
            agent_context=agent_context,
            description=f"Markdown conversion of {filename}",
        )
        if store_error:
            log.warning(
                "convert_to_md: artifact storage failed for %s: %s",
                filename, store_error,
            )

        # ── Extract title ────────────────────────────────────────────────
        title: Optional[str] = None
        for line in md_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and len(stripped) > 2:
                title = stripped[2:].strip()[:255]
                break
        # Fallback: first meaningful line
        if title is None:
            for line in md_content.splitlines():
                stripped = line.strip()
                if len(stripped) > 10 and not stripped.startswith("!"):
                    title = stripped[:255]
                    break

        # ── Build preview ────────────────────────────────────────────────
        # When artifact storage failed, return the full Markdown inline
        # (up to 50K chars) so the agent can still read the content.
        if artifact is None:
            _INLINE_MAX = 50000
            preview = md_content[:_INLINE_MAX]
            preview_truncated = len(md_content) > _INLINE_MAX
        else:
            preview = md_content[:_PREVIEW_CHARS]
            preview_truncated = md_chars > _PREVIEW_CHARS

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "file_id": file_id,
                "source_format": source_format,
                "original_filename": filename,
                "original_size_bytes": file_size,
                "md_size_chars": md_chars,
                "preview": preview,
                "preview_truncated": preview_truncated,
                "md_truncated": md_truncated,
                "artifact": artifact,
                "artifact_error": store_error,
                "title": title,
            },
            execution_time_ms=elapsed,
        )
