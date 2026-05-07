"""gSage AI — generate_document MCP tool.

Generates a document from optional template + Markdown content (or tabular
data for CSV output).

Always runs in background (Celery).

Template sources
----------------
- **No template** (``template_id`` omitted)
    Built-in templates are used:
      * ``output_format="csv"`` — no template, see CSV section.
      * ``pandoc=true`` and ``output_format="pdf"`` — built-in
        ``pandoc_gsage`` bundle (cover page, TOC, gSage colors).
      * Otherwise — built-in ``default`` Markdown template (gSage CSS).
- **Built-in selector** ``template_id="builtin:<name>"``
    Explicitly select a packaged built-in (``default`` or ``pandoc_gsage``).
- **Markdown templates** (``text/markdown``)
    1. ``parse_md_front_matter`` — extract CSS / metadata from front-matter
    2. ``render_jinja2_template`` — inject ``variables`` + ``{"content": content}``
    3. Convert to the requested output format:
        - ``md``   → rendered Markdown (no further conversion)
        - ``html`` → ``md_to_html(css=css)``
        - ``docx`` → ``md_to_html`` → ``html_to_docx``
        - ``pdf``  → ``md_to_html`` → ``html_to_pdf``
- **DOCX templates** (``application/vnd.openxmlformats-officedocument…``)
    1. ``fill_docx_template`` — replace ``{{key}}`` placeholders
    2. Convert to ``docx`` or ``pdf`` (``html``/``md`` not supported).
- **Pandoc bundle (.zip)**
    Multi-file Pandoc bundle containing ``defaults.yaml`` plus a LaTeX
    template, images, etc. Extracted to a sandbox temp dir, then
    ``pandoc --defaults=defaults.yaml`` is invoked there. Only ``pdf``
    output is supported.

CSV output
----------
``output_format="csv"`` does not need a template. Row data is taken from:
  1. The ``rows`` parameter (preferred) — list of dictionaries.
  2. JSON list embedded in ``content`` (auto-detected).
  3. The first GitHub-flavoured Markdown pipe-table found in ``content``.

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
_MIME_CSV = "text/csv"
_MIME_ZIP = "application/zip"

_OUTPUT_FORMATS = ("docx", "pdf", "html", "md", "csv")

# Prefix used in ``template_id`` to explicitly select a built-in template.
_BUILTIN_PREFIX = "builtin:"


class GenerateDocumentTool(BaseTool):
    """
    Generate a document from optional template + Markdown content.

    The tool optionally downloads a template, renders it with the supplied
    content and variables, converts to the desired output format, and
    stores the result as a downloadable file.

    **Template is optional.** When ``template_id`` is omitted, a built-in
    template is selected automatically:

    - ``output_format="csv"`` — no template (rows-to-CSV pipeline).
    - ``pandoc=true`` + ``output_format="pdf"`` — built-in ``pandoc_gsage``
      bundle (LaTeX cover page + TOC + gSage colors).
    - Otherwise — built-in ``default`` Markdown template (gSage CSS).

    Pass ``template_id="builtin:default"`` or ``"builtin:pandoc_gsage"`` to
    explicitly select a packaged built-in.

    **Supported template types:**

    - Markdown templates (``text/markdown``): support all non-CSV output
      formats (``docx``, ``pdf``, ``html``, ``md``).
    - DOCX templates: only support ``docx`` and ``pdf`` output.
    - Pandoc bundles (``application/zip``): multi-file Pandoc bundle with
      ``defaults.yaml``; only ``pdf`` output supported.

    **Conversion** relies on pandoc and (for ``pdf`` via Pandoc bundles) a
    LaTeX engine — both must be installed in the container.

    **Template variables:** Use ``list_templates`` with
    ``include_variables=true`` to discover which ``{{placeholders}}`` a
    template expects. The ``content`` variable is always available.

    **YAML front-matter for PDF cover / title page (pandoc bundles).**
    When generating PDF via the built-in ``pandoc_gsage`` bundle (or any
    Pandoc bundle that enables ``titlepage``), the cover page is rendered
    from the YAML front-matter at the **top of ``content``**. The agent
    should always prepend a front-matter block with the relevant fields,
    for example::

        ---
        title: "Whitepaper \u2013 Topic"
        subtitle: "Optional subtitle"
        author: "Author or team name"
        date: "November 2025"
        subject: "Short subject / abstract line"
        ---

        # Section 1
        …

    Without these fields the resulting PDF will have an empty cover page
    and no document title in the metadata. ``title`` is the most
    important; ``author``, ``date``, ``subtitle`` and ``subject`` are
    optional but recommended.

    **Do NOT manually number sections** when using ``pandoc_gsage`` (or any
    pandoc bundle with ``numbersections: true``). Pandoc auto-numbers all
    headings. Write headings as ``# Scope`` / ``## Subsection`` — never as
    ``# 1. Scope`` or ``## 1.1 Subsection``, otherwise the numbers will
    appear duplicated (e.g. ``1 1. Scope``).
        Markdown content injected into the template (``{{content}}``), or
        the source for CSV output (JSON list / Markdown table) when
        ``rows`` is not supplied.

    Optional parameters
    -------------------
    template_id (str):
        UUID of an uploaded template OR ``"builtin:<name>"``. Omit to use
        the auto-selected built-in.
    output_format (str):
        ``"docx"`` (default), ``"pdf"``, ``"html"``, ``"md"``, or ``"csv"``.
    pandoc (bool):
        When true (and no ``template_id``), use the built-in
        ``pandoc_gsage`` bundle for PDF output. Default false.
    rows (list[object]):
        Tabular data for CSV output. Each item must be a JSON object.
    headers (list[str]):
        Optional explicit column order for CSV output.
    variables (dict):
        Additional template variables merged with ``{"content": content}``.
    output_filename (str):
        Override the output filename base (without extension).

    Permission: ``files:read``, ``files:write``
    Timeout: 120 s · Always background

    **Recovery from lost results.** Because this tool runs in the background,
    the ``ToolResult`` carrying the generated ``file_id`` may not always be
    delivered back to the agent (e.g. long pandoc runs, dropped streams).
    If the agent needs to reference a recently generated document but no
    longer has its ``file_id``, call ``list_recent_artifacts`` (optionally
    with ``tool_name="generate_document"``) to recover the metadata of the
    most recent files produced in the current chat session.
    """

    name: ClassVar[str] = "generate_document"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Generate PDF, DOCX, HTML or CSV documents from Markdown content or templates. "
        "Fenced 'mermaid' and 'dot' blocks become inline diagrams in PDF output (pandoc bundle)."
    )
    category: ClassVar[str] = "document"
    core_tool: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["files:read", "files:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = False
    always_background: ClassVar[bool] = True

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["content"],
        "properties": {
            "template_id": {
                "type": "string",
                "description": (
                    "Optional. UUID of an uploaded template, or "
                    "'builtin:default' / 'builtin:pandoc_gsage' to select a "
                    "packaged built-in. When omitted, a built-in template "
                    "is chosen automatically based on output_format and pandoc."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Markdown content (or JSON / Markdown table for CSV). "
                    "Available as the '{{content}}' variable inside Markdown "
                    "and DOCX templates. "
                    "For PDF via pandoc bundles (e.g. 'builtin:pandoc_gsage' "
                    "or pandoc=true), prepend a YAML front-matter block with "
                    "document metadata so the cover/title page is populated, "
                    "e.g. '---\\ntitle: \"...\"\\nsubtitle: \"...\"\\nauthor: \"...\"\\n"
                    "date: \"...\"\\nsubject: \"...\"\\n---'. Without front-matter "
                    "the cover page will be blank. "
                    "IMPORTANT: do NOT manually number headings (no '# 1. Scope', "
                    "no '## 1.1 Subsection') — the pandoc bundle auto-numbers "
                    "sections; manual numbering causes duplicated numbers like "
                    "'1 1. Scope'. "
                    "DIAGRAMS: when targeting PDF via the pandoc bundle, fenced "
                    "code blocks tagged with a diagram language are rendered as "
                    "inline images. Supported in production: 'mermaid' (Mermaid "
                    "diagrams) and 'dot' (Graphviz). Always validate Mermaid "
                    "diagrams with the 'mermaid_validate' tool BEFORE embedding "
                    "them here. Do NOT include surrounding emojis, HTML tags, or "
                    "non-ASCII control characters inside diagram blocks."
                ),
            },
            "output_format": {
                "type": "string",
                "enum": list(_OUTPUT_FORMATS),
                "description": (
                    "Output format. 'docx' (default), 'pdf', 'html', 'md', "
                    "or 'csv'. DOCX templates only support 'docx'/'pdf'. "
                    "Pandoc bundles (zip) only support 'pdf'. CSV ignores "
                    "any template_id."
                ),
            },
            "pandoc": {
                "type": "boolean",
                "description": (
                    "When true and no template_id is supplied, render PDF "
                    "via the built-in 'pandoc_gsage' Pandoc/LaTeX bundle "
                    "(cover page, TOC, gSage colors, diagram.lua filter for "
                    "mermaid/graphviz diagrams). Ignored otherwise. "
                    "Remember to include YAML front-matter (title, author, "
                    "date, subject, subtitle) at the top of 'content' so the "
                    "cover page is populated."
                ),
            },
            "rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Tabular rows for CSV output. Each item is a JSON object "
                    "(column name -> value). Takes priority over 'content' "
                    "when output_format='csv'."
                ),
            },
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional explicit column order for CSV output. When "
                    "omitted, columns are inferred from the union of keys "
                    "across all rows (first-seen order)."
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
            "scope": {
                "type": "string",
                "enum": ["user", "department"],
                "default": "user",
                "description": (
                    "Visibility scope of the generated file. ALWAYS use 'user' "
                    "(default) UNLESS the user EXPLICITLY asks to share the "
                    "document with their team/department. 'user' keeps the "
                    "document private to the requesting user. 'department' "
                    "makes it visible to ALL members of the user's department — "
                    "only set this when the user explicitly requests sharing. "
                    "Organization-wide scope is not available for generated files. "
                    "If 'department' is set but the user has no department, "
                    "the file falls back to 'user'."
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
            extract_template_zip,
            fill_docx_template,
            find_bundle_defaults_file,
            html_to_docx,
            html_to_pdf,
            markdown_table_to_csv,
            find_latex_unsafe_chars,
            md_to_html,
            pandoc_run_with_defaults,
            parse_md_front_matter,
            render_jinja2_template,
            rows_to_csv,
            strip_non_bmp,
        )
        from src.shared.services.document_templates import (  # noqa: PLC0415
            BUILTIN_DEFAULT_MD,
            BUILTIN_PANDOC_GSAGE,
            get_builtin_pandoc_bundle_dir,
            get_builtin_template_bytes,
        )
        from src.shared.database import _get_session_maker  # noqa: PLC0415

        t0 = time.monotonic()

        # ── Validate params ───────────────────────────────────────────────
        template_id_raw: str = str(params.get("template_id") or "").strip()
        content: str = str(params.get("content", ""))
        output_format: str = str(params.get("output_format") or "docx").lower()
        raw_variables = params.get("variables") or {}
        output_filename_override: Optional[str] = params.get("output_filename")
        use_pandoc: bool = bool(params.get("pandoc") or False)
        rows_param = params.get("rows")
        headers_param = params.get("headers")
        scope_param: str = str(params.get("scope") or "user").strip().lower()
        if scope_param not in ("user", "department"):
            return self._failure(
                "INVALID_INPUT",
                f"'scope' must be 'user' or 'department'. Got: {scope_param!r}",
            )

        # ── Sanitize content for PDF/LaTeX generation ─────────────────────
        # Detect unsafe chars BEFORE stripping so we can warn the agent.
        unsafe_chars = find_latex_unsafe_chars(content) if content else []
        if unsafe_chars:
            samples = ", ".join(
                f"{c} (U+{ord(c):04X})" for c in unsafe_chars[:10]
            )
            log.warning(
                "generate_document: stripping %d LaTeX-unsafe char(s) from content: %s",
                len(unsafe_chars),
                samples,
            )
        content = strip_non_bmp(content)

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

        # ── CSV pipeline (no template) ────────────────────────────────────
        if output_format == "csv":
            try:
                csv_bytes = _build_csv_bytes(
                    rows_param=rows_param,
                    headers_param=headers_param,
                    content=content,
                    rows_to_csv=rows_to_csv,
                    markdown_table_to_csv=markdown_table_to_csv,
                )
            except ValueError as exc:
                elapsed = int((time.monotonic() - t0) * 1000)
                return self._failure(
                    "INVALID_INPUT",
                    f"Cannot build CSV: {exc}",
                    execution_time_ms=elapsed,
                )

            base_name = output_filename_override or "report"
            return await self._store_and_return(
                output_bytes=csv_bytes,
                out_content_type=_MIME_CSV,
                out_ext="csv",
                base_name=base_name,
                template_label="csv",
                template_id_label=template_id_raw or "csv",
                output_format=output_format,
                agent_context=agent_context,
                t0=t0,
                session_maker_factory=_get_session_maker,
                scope=scope_param,
            )

        # ── Resolve template source ──────────────────────────────────────
        try:
            template_bytes, template_filename, template_content_type = await self._resolve_template(
                template_id_raw=template_id_raw,
                output_format=output_format,
                use_pandoc=use_pandoc,
                agent_context=agent_context,
                get_builtin_template_bytes=get_builtin_template_bytes,
                get_builtin_pandoc_bundle_dir=get_builtin_pandoc_bundle_dir,
                BUILTIN_DEFAULT_MD=BUILTIN_DEFAULT_MD,
                BUILTIN_PANDOC_GSAGE=BUILTIN_PANDOC_GSAGE,
            )
        except _TemplateNotFound as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "TEMPLATE_NOT_FOUND", str(exc), execution_time_ms=elapsed
            )
        except _UnsupportedOutputFormat as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "UNSUPPORTED_OUTPUT_FORMAT", str(exc), execution_time_ms=elapsed
            )
        except _BuiltinPandocBundle as bundle:
            # Special path: render directly from the on-disk built-in bundle.
            try:
                pdf_bytes = await pandoc_run_with_defaults(
                    input_md=content,
                    bundle_dir=str(bundle.bundle_dir),
                )
            except FileNotFoundError as exc:
                elapsed = int((time.monotonic() - t0) * 1000)
                return self._failure(
                    "PANDOC_NOT_FOUND",
                    f"pandoc or built-in bundle missing: {exc}",
                    retryable=False,
                    execution_time_ms=elapsed,
                )
            except RuntimeError as exc:
                elapsed = int((time.monotonic() - t0) * 1000)
                return self._failure(
                    "PANDOC_ERROR",
                    f"pandoc conversion failed: {exc}",
                    retryable=False,
                    execution_time_ms=elapsed,
                )

            base_name = output_filename_override or "document"
            return await self._store_and_return(
                output_bytes=pdf_bytes,
                out_content_type=_MIME_PDF,
                out_ext="pdf",
                base_name=base_name,
                template_label=f"builtin:{BUILTIN_PANDOC_GSAGE}",
                template_id_label=f"builtin:{BUILTIN_PANDOC_GSAGE}",
                output_format=output_format,
                agent_context=agent_context,
                t0=t0,
                session_maker_factory=_get_session_maker,
                scope=scope_param,
            )

        base_name = output_filename_override or _stem(template_filename)

        # ── Validate template variables (Markdown templates only) ─────────
        is_md_template = template_content_type == _MIME_MD
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
                template_filename=template_filename,
                template_content_type=template_content_type,
                output_format=output_format,
                variables=variables,
                content=content,
                render_jinja2_template=render_jinja2_template,
                parse_md_front_matter=parse_md_front_matter,
                md_to_html=md_to_html,
                html_to_docx=html_to_docx,
                html_to_pdf=html_to_pdf,
                fill_docx_template=fill_docx_template,
                docx_to_pdf=docx_to_pdf,
                extract_template_zip=extract_template_zip,
                find_bundle_defaults_file=find_bundle_defaults_file,
                pandoc_run_with_defaults=pandoc_run_with_defaults,
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
            log.error(
                "generate_document: pandoc error for template %s: %s",
                template_id_raw or "<builtin>", exc,
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "PANDOC_ERROR",
                f"pandoc conversion failed: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except ValueError as exc:
            # Bad ZIP / unsafe paths / etc.
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_TEMPLATE",
                f"Invalid template content: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception(
                "generate_document: conversion error for template %s",
                template_id_raw or "<builtin>",
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "CONVERSION_FAILED",
                f"Document conversion failed: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )

        return await self._store_and_return(
            output_bytes=output_bytes,
            out_content_type=out_content_type,
            out_ext=out_ext,
            base_name=base_name,
            template_label=template_filename,
            template_id_label=template_id_raw or f"builtin:{BUILTIN_DEFAULT_MD}",
            output_format=output_format,
            agent_context=agent_context,
            t0=t0,
            session_maker_factory=_get_session_maker,
            scope=scope_param,
        )

    # ------------------------------------------------------------------
    # Helpers (instance methods kept here to reuse self._load_file / self._store_file)
    # ------------------------------------------------------------------

    async def _resolve_template(
        self,
        *,
        template_id_raw: str,
        output_format: str,
        use_pandoc: bool,
        agent_context: AgentContext,
        get_builtin_template_bytes,
        get_builtin_pandoc_bundle_dir,
        BUILTIN_DEFAULT_MD: str,
        BUILTIN_PANDOC_GSAGE: str,
    ) -> tuple[bytes, str, str]:
        """Resolve the template source.

        Returns ``(template_bytes, template_filename, template_content_type)``.
        Raises :class:`_BuiltinPandocBundle` to signal a built-in pandoc-bundle
        path (handled separately by the caller because it does not need
        in-memory template bytes), or :class:`_TemplateNotFound` when an
        uploaded template can't be loaded.
        """
        # Explicit built-in selector
        if template_id_raw.startswith(_BUILTIN_PREFIX):
            name = template_id_raw[len(_BUILTIN_PREFIX):].strip().lower()
            if name == BUILTIN_PANDOC_GSAGE:
                if output_format != "pdf":
                    raise _UnsupportedOutputFormat(
                        "Built-in 'pandoc_gsage' bundle only supports 'pdf' output."
                    )
                raise _BuiltinPandocBundle(get_builtin_pandoc_bundle_dir())
            if name == BUILTIN_DEFAULT_MD:
                return (
                    get_builtin_template_bytes(BUILTIN_DEFAULT_MD),
                    f"{BUILTIN_DEFAULT_MD}.md",
                    _MIME_MD,
                )
            raise _TemplateNotFound(f"Unknown built-in template: {template_id_raw!r}")

        # No template_id → auto-select a built-in
        if not template_id_raw:
            if use_pandoc and output_format == "pdf":
                raise _BuiltinPandocBundle(get_builtin_pandoc_bundle_dir())
            return (
                get_builtin_template_bytes(BUILTIN_DEFAULT_MD),
                f"{BUILTIN_DEFAULT_MD}.md",
                _MIME_MD,
            )

        # User-uploaded template
        load_result = await self._load_file(
            file_id=template_id_raw,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=10 * 1024 * 1024,
        )
        if load_result is None:
            raise _TemplateNotFound(
                f"Template '{template_id_raw}' not found or access denied."
            )
        return (
            load_result["data"],
            load_result["filename"],
            load_result["content_type"],
        )

    async def _store_and_return(
        self,
        *,
        output_bytes: bytes,
        out_content_type: str,
        out_ext: str,
        base_name: str,
        template_label: str,
        template_id_label: str,
        output_format: str,
        agent_context: AgentContext,
        t0: float,
        session_maker_factory,
        scope: str = "user",
    ) -> ToolResult:
        """Store the generated bytes as a GSageFile and return a ToolResult."""
        file_info: Optional[dict] = None
        try:
            async with session_maker_factory()() as db_session:
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
                        f"Generated from template '{template_label}' "
                        f"as {output_format.upper()}"
                    ),
                    scope=scope,
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
                "template_id": template_id_label,
                "output_format": output_format,
            },
            execution_time_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _UnsupportedOutputFormat(Exception):
    """Raised when the requested output format is incompatible with the template type."""


class _TemplateNotFound(Exception):
    """Raised when an uploaded template UUID can't be loaded."""


class _BuiltinPandocBundle(Exception):
    """Sentinel exception carrying the built-in Pandoc bundle directory.

    Raised by :meth:`GenerateDocumentTool._resolve_template` when the
    request maps to the built-in pandoc bundle. The caller catches it,
    runs pandoc directly against the on-disk bundle, and returns.
    """

    def __init__(self, bundle_dir):
        self.bundle_dir = bundle_dir
        super().__init__(f"Use built-in pandoc bundle at {bundle_dir!r}")


def _build_csv_bytes(
    *,
    rows_param,
    headers_param,
    content: str,
    rows_to_csv,
    markdown_table_to_csv,
) -> bytes:
    """Resolve CSV rows from explicit param / JSON content / Markdown table."""
    import json  # noqa: PLC0415

    headers: Optional[list[str]] = None
    if isinstance(headers_param, list):
        headers = [str(h) for h in headers_param]

    # 1. Explicit rows parameter
    if isinstance(rows_param, list) and rows_param:
        rows = [r for r in rows_param if isinstance(r, dict)]
        if not rows:
            raise ValueError("'rows' must contain at least one JSON object.")
        return rows_to_csv(rows, headers=headers)

    # 2. JSON list embedded in content
    stripped = content.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and parsed and all(isinstance(x, dict) for x in parsed):
            return rows_to_csv(parsed, headers=headers)

    # 3. Markdown pipe-table fallback
    return markdown_table_to_csv(content)


async def _convert(
    *,
    template_bytes: bytes,
    template_filename: str,
    template_content_type: str,
    output_format: str,
    variables: dict,
    content: str,
    render_jinja2_template,
    parse_md_front_matter,
    md_to_html,
    html_to_docx,
    html_to_pdf,
    fill_docx_template,
    docx_to_pdf,
    extract_template_zip,
    find_bundle_defaults_file,
    pandoc_run_with_defaults,
) -> tuple[bytes, str, str]:
    """Dispatch to the correct conversion pipeline.

    Returns ``(output_bytes, content_type, extension)``.

    Raises
    ------
    _UnsupportedOutputFormat
    FileNotFoundError
    RuntimeError
    ValueError
    """
    is_zip_bundle = (
        template_content_type == "application/zip"
        and template_filename.lower().endswith(".zip")
    )

    if is_zip_bundle:
        return await _pipeline_pandoc_bundle(
            zip_bytes=template_bytes,
            content=content,
            output_format=output_format,
            extract_template_zip=extract_template_zip,
            find_bundle_defaults_file=find_bundle_defaults_file,
            pandoc_run_with_defaults=pandoc_run_with_defaults,
        )

    is_docx_template = template_content_type == _MIME_DOCX or (
        template_content_type == "application/zip"
        and template_filename.lower().endswith(".docx")
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


async def _pipeline_pandoc_bundle(
    *,
    zip_bytes: bytes,
    content: str,
    output_format: str,
    extract_template_zip,
    find_bundle_defaults_file,
    pandoc_run_with_defaults,
) -> tuple[bytes, str, str]:
    """Pandoc bundle (.zip) → PDF.

    Extracts the ZIP into a temporary directory (zip-slip safe), locates
    ``defaults.yaml``, then runs pandoc with ``cwd`` set to that directory
    so all relative paths inside the bundle resolve correctly.
    """
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    if output_format != "pdf":
        raise _UnsupportedOutputFormat(
            "Pandoc bundles (.zip) only support 'pdf' output. "
            f"Requested: '{output_format}'."
        )

    with tempfile.TemporaryDirectory(prefix="gsage_pandoc_") as tmp:
        bundle_root = extract_template_zip(zip_bytes, tmp)
        defaults_path = find_bundle_defaults_file(bundle_root)
        if defaults_path is None:
            raise ValueError(
                "Pandoc bundle is missing 'defaults.yaml' (or 'defaults.yml')."
            )
        defaults_filename = os.path.basename(defaults_path)
        pdf_bytes = await pandoc_run_with_defaults(
            input_md=content,
            bundle_dir=bundle_root,
            defaults_filename=defaults_filename,
        )
    return pdf_bytes, _MIME_PDF, "pdf"


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
