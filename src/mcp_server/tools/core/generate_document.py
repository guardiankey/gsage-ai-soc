"""gSage AI — generate_document MCP tool.

Generates a document from a template and Markdown content.

Always runs in background (Celery).

Template types
--------------
- **Markdown templates** (``text/markdown``):
    1. ``parse_md_front_matter`` — extract CSS / metadata from front-matter
    2. ``render_jinja2_template`` — inject ``variables`` + ``{"content": content}``
    3. Convert to the requested output format:
        - ``md``   → rendered Markdown (no further conversion)
        - ``html`` → ``md_to_html(css=css)``
        - ``docx`` → ``md_to_html`` → ``html_to_docx``
        - ``pdf``  → ``md_to_html`` → ``html_to_pdf``

- **DOCX templates** (``application/vnd.openxmlformats-officedocument…``):
    1. ``fill_docx_template`` — replace ``{{key}}`` placeholders
    2. Convert to the requested output format:
        - ``docx`` → filled DOCX (no further conversion)
        - ``pdf``  → ``docx_to_pdf``
        - ``html`` / ``md`` → error (UNSUPPORTED_OUTPUT_FORMAT)

Required permissions: ``files:read``, ``files:write``
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, ClassVar, Optional

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_MIME_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_MIME_PDF = "application/pdf"
_MIME_HTML = "text/html"
_MIME_MD = "text/markdown"

_OUTPUT_FORMATS = ("docx", "pdf", "html", "md")


class GenerateDocumentTool(BaseTool):
    """
    Generate a document from a template and Markdown content.

    The tool downloads the specified template, renders it with the supplied
    content and optional variables, converts to the desired output format,
    and stores the result as a downloadable file.

    **Supported template types:**

    - Markdown templates (``text/markdown``): support all output formats
      (``docx``, ``pdf``, ``html``, ``md``).
    - DOCX templates: only support ``docx`` and ``pdf`` output.
      Requesting ``html`` or ``md`` from a DOCX template returns an error.

    **Conversion** relies on pandoc — it must be installed in the container.

    **Template variables:** Use ``list_templates`` with
    ``include_variables=true`` to discover which ``{{placeholders}}`` a
    template expects.  The ``content`` variable is always available; any
    additional variables must be passed via the ``variables`` parameter.
    If a required variable is missing, the tool returns a
    ``MISSING_VARIABLES`` error listing the expected names.

    Required parameters
    -------------------
    template_id (str):
        UUID of the template file (use ``list_templates`` to find it).
    content (str):
        Markdown content injected into the template via the
        ``{{content}}`` placeholder (or as the body for Markdown templates).

    Optional parameters
    -------------------
    output_format (str):
        ``"docx"`` (default), ``"pdf"``, ``"html"``, or ``"md"``.
    variables (dict):
        Additional template variables merged with ``{"content": content}``.
        Use ``list_templates(include_variables=true)`` to discover which
        variables a template expects.
    output_filename (str):
        Override the output filename base (without extension).

    Permission: ``files:read``, ``files:write``
    Timeout: 120 s · Always background
    """

    name: ClassVar[str] = "generate_document"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Generate documents (PDF, DOCX, HTML) from Markdown content or DOCX templates"
    category: ClassVar[str] = "document"
    core_tool: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["files:read", "files:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = False
    always_background: ClassVar[bool] = True

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["template_id", "content"],
        "properties": {
            "template_id": {
                "type": "string",
                "description": (
                    "UUID of the template to use. "
                    "Obtain valid IDs from the 'list_templates' tool."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Markdown content to inject into the template. "
                    "Available as the '{{content}}' variable inside the template."
                ),
            },
            "output_format": {
                "type": "string",
                "enum": list(_OUTPUT_FORMATS),
                "description": (
                    "Output format. 'docx' (default), 'pdf', 'html', or 'md'. "
                    "DOCX templates only support 'docx' and 'pdf'."
                ),
            },
            "variables": {
                "type": "object",
                "description": (
                    "Additional template variables merged with {'content': content}. "
                    "Values must be strings or coercible to string."
                ),
                "additionalProperties": {"type": "string"},
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "Base filename for the generated file (without extension). "
                    "Defaults to the template filename without its original extension."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        from jinja2 import TemplateError, UndefinedError  # noqa: PLC0415
        from src.shared.services.document_converter import (  # noqa: PLC0415
            docx_to_pdf,
            extract_template_variables,
            fill_docx_template,
            html_to_docx,
            html_to_pdf,
            md_to_html,
            parse_md_front_matter,
            render_jinja2_template,
        )
        from src.shared.database import _get_session_maker  # noqa: PLC0415

        t0 = time.monotonic()

        # ── Validate params ───────────────────────────────────────────────
        template_id: str = str(params.get("template_id", "")).strip()
        content: str = str(params.get("content", ""))
        output_format: str = str(params.get("output_format") or "docx").lower()
        raw_variables = params.get("variables") or {}
        output_filename_override: Optional[str] = params.get("output_filename")

        if not template_id:
            return self._failure("INVALID_INPUT", "'template_id' is required.")
        if output_format not in _OUTPUT_FORMATS:
            return self._failure(
                "INVALID_INPUT",
                f"'output_format' must be one of: {', '.join(_OUTPUT_FORMATS)}. Got: {output_format!r}",
            )

        variables: dict = (
            {str(k): str(v) for k, v in raw_variables.items()}
            if isinstance(raw_variables, dict)
            else {}
        )
        variables["content"] = content

        # ── Load template from MinIO ──────────────────────────────────────
        load_result = await self._load_file(
            file_id=template_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=10 * 1024 * 1024,
        )

        if load_result is None:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "TEMPLATE_NOT_FOUND",
                f"Template '{template_id}' not found or access denied.",
                execution_time_ms=elapsed,
            )

        template_bytes: bytes = load_result["data"]
        template_filename: str = load_result["filename"]
        template_content_type: str = load_result["content_type"]

        base_name = output_filename_override or _stem(template_filename)

        # ── Validate template variables ───────────────────────────────────
        # For Markdown templates, extract expected variables and warn about
        # any that are missing from the supplied `variables` dict.
        is_md_template = template_content_type not in (_MIME_DOCX, "application/zip")
        if is_md_template:
            template_text = template_bytes.decode("utf-8", errors="replace")
            expected_vars = extract_template_variables(template_text)
            missing = [v for v in expected_vars if v not in variables]
            if missing:
                elapsed = int((time.monotonic() - t0) * 1000)
                return self._failure(
                    "MISSING_VARIABLES",
                    (
                        f"The template expects the following variables that were not "
                        f"provided: {', '.join(missing)}. "
                        f"All expected variables: {', '.join(expected_vars)}. "
                        f"Pass them via the 'variables' parameter."
                    ),
                    execution_time_ms=elapsed,
                )

        # ── Convert ───────────────────────────────────────────────────────
        try:
            output_bytes, out_content_type, out_ext = await _convert(
                template_bytes=template_bytes,
                template_content_type=template_content_type,
                output_format=output_format,
                variables=variables,
                render_jinja2_template=render_jinja2_template,
                parse_md_front_matter=parse_md_front_matter,
                md_to_html=md_to_html,
                html_to_docx=html_to_docx,
                html_to_pdf=html_to_pdf,
                fill_docx_template=fill_docx_template,
                docx_to_pdf=docx_to_pdf,
            )
        except _UnsupportedOutputFormat as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "UNSUPPORTED_OUTPUT_FORMAT",
                str(exc),
                execution_time_ms=elapsed,
            )
        except FileNotFoundError:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "PANDOC_NOT_FOUND",
                "pandoc is not installed. Contact your system administrator.",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except UndefinedError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "TEMPLATE_VARIABLE_ERROR",
                f"Template rendering failed — undefined variable: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except TemplateError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "TEMPLATE_RENDER_ERROR",
                f"Jinja2 template rendering error: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except RuntimeError as exc:
            # pandoc exits with non-zero status → RuntimeError from _run_pandoc
            log.error(
                "generate_document: pandoc error for template %s: %s",
                template_id, exc,
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "PANDOC_ERROR",
                f"pandoc conversion failed: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception(
                "generate_document: conversion error for template %s", template_id
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "CONVERSION_FAILED",
                f"Document conversion failed: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )

        # ── Store generated file ──────────────────────────────────────────
        file_info: Optional[dict] = None
        try:
            async with _get_session_maker()() as db_session:
                output_name = await _resolve_unique_filename(
                    db_session,
                    agent_context.org_id,
                    agent_context.user_id,
                    base_name,
                    out_ext,
                )
                file_info = await self._store_file(
                    data=output_bytes,
                    filename=output_name,
                    content_type=out_content_type,
                    agent_context=agent_context,
                    session=db_session,
                    description=(
                        f"Generated from template '{template_filename}' "
                        f"as {output_format.upper()}"
                    ),
                )
        except Exception as exc:
            log.error("generate_document: failed to store result file: %s", exc)

        if file_info is None:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "STORE_FAILED",
                "Document was generated but could not be saved. Try again later.",
                retryable=True,
                execution_time_ms=elapsed,
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "file": file_info,
                "template_id": template_id,
                "output_format": output_format,
            },
            execution_time_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _UnsupportedOutputFormat(Exception):
    """Raised when the requested output format is incompatible with the template type."""


async def _convert(
    *,
    template_bytes: bytes,
    template_content_type: str,
    output_format: str,
    variables: dict,
    render_jinja2_template,
    parse_md_front_matter,
    md_to_html,
    html_to_docx,
    html_to_pdf,
    fill_docx_template,
    docx_to_pdf,
) -> tuple[bytes, str, str]:
    """Dispatch to the correct conversion pipeline.

    Returns ``(output_bytes, content_type, extension)``.

    Raises
    ------
    _UnsupportedOutputFormat
    FileNotFoundError
    RuntimeError
    """
    is_docx_template = template_content_type in (
        _MIME_DOCX,
        "application/zip",
    )

    if is_docx_template:
        return await _pipeline_docx(
            docx_bytes=template_bytes,
            output_format=output_format,
            variables=variables,
            fill_docx_template=fill_docx_template,
            docx_to_pdf=docx_to_pdf,
        )

    # Default: treat as Markdown template
    return await _pipeline_md(
        template_text=template_bytes.decode("utf-8", errors="replace"),
        output_format=output_format,
        variables=variables,
        render_jinja2_template=render_jinja2_template,
        parse_md_front_matter=parse_md_front_matter,
        md_to_html=md_to_html,
        html_to_docx=html_to_docx,
        html_to_pdf=html_to_pdf,
    )


async def _pipeline_md(
    *,
    template_text: str,
    output_format: str,
    variables: dict,
    render_jinja2_template,
    parse_md_front_matter,
    md_to_html,
    html_to_docx,
    html_to_pdf,
) -> tuple[bytes, str, str]:
    """Markdown template → requested output format."""
    metadata, body = parse_md_front_matter(template_text)
    css: Optional[str] = metadata.get("css") or None

    rendered_md = render_jinja2_template(body, variables)

    if output_format == "md":
        return rendered_md.encode("utf-8"), _MIME_MD, "md"

    html = md_to_html(rendered_md, css=css)

    if output_format == "html":
        return html.encode("utf-8"), _MIME_HTML, "html"

    if output_format == "docx":
        docx_bytes = await html_to_docx(html)
        return docx_bytes, _MIME_DOCX, "docx"

    # pdf
    pdf_bytes = await html_to_pdf(html)
    return pdf_bytes, _MIME_PDF, "pdf"


async def _pipeline_docx(
    *,
    docx_bytes: bytes,
    output_format: str,
    variables: dict,
    fill_docx_template,
    docx_to_pdf,
) -> tuple[bytes, str, str]:
    """DOCX template → requested output format."""
    if output_format in ("html", "md"):
        raise _UnsupportedOutputFormat(
            "DOCX templates only support 'docx' and 'pdf' output formats. "
            f"Requested: '{output_format}'."
        )

    filled = fill_docx_template(docx_bytes, variables)

    if output_format == "docx":
        return filled, _MIME_DOCX, "docx"

    # pdf
    pdf_bytes = await docx_to_pdf(filled)
    return pdf_bytes, _MIME_PDF, "pdf"


def _stem(filename: str) -> str:
    """Return filename without extension."""
    if "." in filename:
        return filename.rsplit(".", 1)[0]
    return filename


async def _resolve_unique_filename(
    session: "AsyncSession",
    org_id: "uuid.UUID",
    user_id: "uuid.UUID",
    base_name: str,
    ext: str,
) -> str:
    """Return ``base_name.ext``, adding a ``_N`` suffix when the name is already taken.

    Queries non-purged files owned by *user_id* within *org_id* to discover
    any existing names that share the same base, then returns the first
    available candidate: ``base_name.ext``, ``base_name_1.ext``,
    ``base_name_2.ext``, …
    """
    import uuid  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415
    from src.shared.models.generated_file import GSageFile  # noqa: PLC0415

    result = await session.execute(
        select(GSageFile.filename).where(
            GSageFile.org_id == org_id,
            GSageFile.user_id == user_id,
            GSageFile.purged_at.is_(None),
            GSageFile.filename.like(f"{base_name}%.{ext}"),
        )
    )
    existing: set[str] = {row[0] for row in result.fetchall()}

    candidate = f"{base_name}.{ext}"
    if candidate not in existing:
        return candidate

    counter = 1
    while True:
        candidate = f"{base_name}_{counter}.{ext}"
        if candidate not in existing:
            return candidate
        counter += 1
