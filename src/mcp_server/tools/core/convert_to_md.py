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
_PREVIEW_CHARS = 300  # Fixed inline preview size (kept small to avoid
                       # blowing up LLM context; the full content is always
                       # available as a downloadable artifact).
_OCR_MAX_PAGES = 50  # Max PDF pages to process via OCR (limits timeout risk)

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


def _pdf_has_extractable_text(
    data: bytes,
    min_chars: int = 500,
    min_text_ratio: float = 0.3,
    max_pages_to_check: int = 20,
) -> bool:
    """Return True if PDF has enough extractable text to use markitdown.

    Uses two combined heuristics to avoid false positives from residual
    text (digital signatures, stamps, headers on otherwise image-only pages):

    1. Total extracted chars across checked pages >= *min_chars* (default 500).
    2. Ratio of pages-with-text / checked-pages >= *min_text_ratio* (default 0.3).

    Both must be satisfied for the PDF to be considered text-based.

    Only the first *max_pages_to_check* pages are examined (default 20).
    The function exits early as soon as a definitive answer is reached.
    """
    import io
    import logging as _logging

    # ── Suppress pdfminer DEBUG spam ────────────────────────────────────
    # pdfplumber uses pdfminer internally; at DEBUG level it logs every
    # PDF content-stream token, producing tens of thousands of lines per
    # page.  This I/O alone can cause timeouts even for small PDFs.
    _logging.getLogger("pdfminer").setLevel(_logging.WARNING)

    import pdfplumber  # noqa: PLC0415

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
        pages = pdf.pages
        if not pages:
            pdf.close()
            return False

        total_pages = len(pages)
        pages_to_check = min(total_pages, max_pages_to_check)

        total_chars = 0
        pages_with_text = 0
        pages_checked = 0

        for page in pages[:pages_to_check]:
            try:
                text = page.extract_text() or ""
                chars = len(text.strip())
                total_chars += chars
                pages_checked += 1
                if chars > 50:  # meaningful content, not just a stamp
                    pages_with_text += 1
            except Exception:
                pages_checked += 1
                continue

            # ── Early exit: already proven text-based ───────────────────
            if total_chars >= min_chars and (
                pages_with_text / pages_checked >= min_text_ratio
            ):
                pdf.close()
                log.debug(
                    "_pdf_has_extractable_text: early YES at page %d/%d "
                    "(chars=%d, text_pages=%d)",
                    pages_checked, pages_to_check, total_chars, pages_with_text,
                )
                return True

            # ── Early exit: impossible to reach ratio ────────────────────
            # Even if all remaining pages had text, we couldn't hit min_text_ratio
            remaining = pages_to_check - pages_checked
            best_possible = (pages_with_text + remaining) / pages_to_check
            if best_possible < min_text_ratio and total_chars < min_chars:
                pdf.close()
                log.debug(
                    "_pdf_has_extractable_text: early NO at page %d/%d "
                    "(best_possible_ratio=%.2f)",
                    pages_checked, pages_to_check, best_possible,
                )
                return False

        pdf.close()

        text_ratio = (
            pages_with_text / pages_checked if pages_checked > 0 else 0
        )
        result = total_chars >= min_chars and text_ratio >= min_text_ratio

        log.debug(
            "_pdf_has_extractable_text: chars=%d pages_with_text=%d/%d "
            "(of %d total) ratio=%.2f -> %s",
            total_chars, pages_with_text, pages_checked, total_pages,
            text_ratio, result,
        )
        return result

    except Exception:
        # If pdfplumber can't open it, fall back to markitdown
        log.warning(
            "_pdf_has_extractable_text: pdfplumber failed, falling back to markitdown",
        )
        return True


def _get_docling_version() -> str | None:
    """Return the installed Docling version string, or None."""
    try:
        from importlib.metadata import version  # noqa: PLC0415
        return version("docling")
    except Exception:
        return None


def _convert_pdf_ocr(data: bytes, engine: str = "docling") -> tuple[str, dict]:
    """Convert image-based PDF to Markdown via OCR.

    Args:
        data: Raw PDF bytes.
        engine: OCR engine to use. Currently only ``"docling"`` is supported.

    Returns:
        ``(markdown_text, pipeline_meta_dict)``.
        *pipeline_meta* includes: ``engine``, ``ocr_applied``, ``engine_version``.

    Raises:
        ValueError: If *engine* is not supported.
    """
    import tempfile

    pipeline_meta: dict = {
        "engine": engine,
        "ocr_applied": True,
        "engine_version": None,
    }

    if engine != "docling":
        raise ValueError(f"Unsupported OCR engine: {engine!r}")

    # ── Redirect model/cache dirs away from $HOME ───────────────────────
    # Docling depends on huggingface_hub / transformers / torch, which all
    # try to write to ~/.cache/.  In the container, appuser is a system
    # user (useradd -r) whose $HOME may be missing or non‑writable.
    # Point everything at /tmp so Docling can download its models.
    import os

    _ocr_cache = "/tmp/docling_cache"
    os.makedirs(_ocr_cache, exist_ok=True)
    os.environ.setdefault("HF_HOME", os.path.join(_ocr_cache, "huggingface"))
    os.environ.setdefault("TORCH_HOME", os.path.join(_ocr_cache, "torch"))
    os.environ.setdefault("XDG_CACHE_HOME", _ocr_cache)

    # Lazy import — Docling is heavy (~PyTorch + models)
    from docling.document_converter import DocumentConverter  # noqa: PLC0415

    pipeline_meta["engine_version"] = _get_docling_version()

    # Docling's convert() expects a path/URL string, not BytesIO.
    # Write to a temporary file so Docling can read it.
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(data)
        tmp.close()

        converter = DocumentConverter()
        result = converter.convert(tmp.name)
        md_content = result.document.export_to_markdown()
    finally:
        import os
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    return md_content, pipeline_meta


# ── Tool ─────────────────────────────────────────────────────────────────────


class ConvertToMdTool(BaseTool):
    """Convert a document attachment (PDF, DOCX, PPTX, XLSX, HTML, RTF) to Markdown.

    Provide the ``file_id`` of an attachment already uploaded to the chat.
    The tool detects the format automatically, converts the content to
    Markdown, saves it as a downloadable artifact, and returns a short
    inline preview (~300 characters).

    Plain-text files (TXT, CSV, JSON, XML, …) are passed through unchanged.
    """

    name: ClassVar[str] = "convert_to_md"
    version: ClassVar[str] = "1.2.0"
    summary: ClassVar[str] = (
        "Convert an attached document (PDF, DOCX, PPTX, XLSX, HTML, RTF, TXT) "
        "to Markdown. PDFs are auto-detected as text or image; image PDFs "
        "use OCR (Docling). Saves the result as a downloadable artifact "
        "and returns a short preview."
    )
    category: ClassVar[str] = "file"
    permissions: ClassVar[list[str]] = ["core:convert_md"]
    rate_limit_per_minute: ClassVar[int] = 15
    timeout_seconds: ClassVar[int] = 90
    requires_approval: ClassVar[bool] = False
    use_circuit_breaker: ClassVar[bool] = False
    # Dispatch to Celery background worker when synchronous execution
    # exceeds this threshold instead of returning a hard TOOL_TIMEOUT.
    background_threshold_seconds: ClassVar[Optional[int]] = 60

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
            "force_ocr": {
                "type": "boolean",
                "description": (
                    "Force OCR processing even if the PDF has extractable text. "
                    "Useful when text extraction is corrupted or poorly formatted. "
                    "Default: false."
                ),
            },
            "ocr_engine": {
                "type": "string",
                "enum": ["docling"],
                "description": (
                    "OCR engine to use for image-based PDFs. "
                    "Currently only 'docling' is supported. Default: 'docling'."
                ),
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "Optional custom filename for the output Markdown file. "
                    "If omitted, defaults to 'convert_{timestamp}_{original_name}.md'. "
                    "The '.md' extension is appended automatically if missing. "
                    "Non-alphanumeric characters (except . _ - and space) are "
                    "replaced with underscores. Example: 'meeting-notes.md', "
                    "'report-final'."
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
            log.warning(
                "convert_to_md: _store_or_fallback — no DB session in context; "
                "cannot persist artifact %r",
                filename,
            )
            return None, "no DB session available"

        log.debug(
            "convert_to_md: _store_or_fallback — storing %d bytes as %r (%s)",
            len(data), filename, content_type,
        )

        try:
            artifact = await self._store_file(
                data=data,
                filename=filename,
                content_type=content_type,
                agent_context=agent_context,
                session=session,
                description=description,
            )
        except Exception as exc:
            log.exception(
                "convert_to_md: _store_file raised for %r: %s",
                filename, exc,
            )
            return None, f"_store_file exception: {exc}"

        if artifact is None:
            # _store_file returned None without raising — it caught an
            # internal error (MinIO upload failure, DB commit failure,
            # closed/idle session, etc.) and already logged it.
            log.warning(
                "convert_to_md: _store_file returned None for %r "
                "(check MCP server logs for MinIO/DB errors)",
                filename,
            )
            return None, (
                "Artifact storage failed — _store_file returned None. "
                "Check MCP server logs for MinIO or database errors."
            )

        log.debug(
            "convert_to_md: artifact stored successfully — file_id=%s size=%d",
            artifact.get("file_id"), artifact.get("size_bytes"),
        )
        return artifact, None

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
        pipeline_meta: Optional[dict] = None

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
            elif source_format == "pdf":
                force_ocr = params.get("force_ocr", False)
                ocr_engine = params.get("ocr_engine", "docling")
                pipeline_meta = {"ocr_applied": False, "engine": "markitdown"}

                if not force_ocr and _pdf_has_extractable_text(data):
                    md_content = _convert_with_markitdown(data)
                    log.info(
                        "convert_to_md: %s is text-based PDF, using markitdown",
                        filename,
                    )
                else:
                    md_content, pipeline_meta = _convert_pdf_ocr(
                        data, engine=ocr_engine,
                    )
                    log.info(
                        "convert_to_md: %s using OCR (engine=%s, force=%s)",
                        filename, ocr_engine, force_ocr,
                    )
            else:
                md_content = _convert_with_markitdown(data)
                pipeline_meta = {"ocr_applied": False, "engine": "markitdown"}
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
        output_filename: str | None = (
            (params.get("output_filename") or "").strip() or None
        )
        if output_filename:
            safe = "".join(
                c if c.isalnum() or c in "._- " else "_" for c in output_filename
            )[:200]
            if not safe.lower().endswith(".md"):
                safe += ".md"
            artifact_filename = safe
        else:
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
        # ALWAYS keep the preview small (~300 chars).  The full content is
        # meant to be consumed via the downloadable artifact.  Blowing up
        # the LLM context with a 50 KB inline preview causes more problems
        # than it solves.
        preview = md_content[:_PREVIEW_CHARS]
        preview_truncated = md_chars > _PREVIEW_CHARS

        # ── Build debug info (helps diagnose storage issues) ─────────────
        debug_info: dict = {
            "storage_attempted": True,
            "storage_ok": artifact is not None,
            "storage_error": store_error,
        }
        if not debug_info["storage_ok"]:
            debug_info["storage_hint"] = (
                "The Markdown was generated successfully (%d chars) but "
                "could not be persisted as a downloadable artifact. "
                "Check MCP server logs for MinIO connectivity or database "
                "errors (idle session timeout, connection refused, etc.). "
                "The inline preview below contains the first %d characters."
            ) % (md_chars, _PREVIEW_CHARS)

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
                "pipeline": pipeline_meta,
                "debug_info": debug_info,
            },
            execution_time_ms=elapsed,
        )
