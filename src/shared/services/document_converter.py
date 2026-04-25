"""gSage AI — Document conversion utilities.

Stateless helpers that convert between Markdown, HTML, DOCX and PDF.
All functions are pure (no DB, no MinIO) and can be called from tools or tasks.

PDF and DOCX conversion requires pandoc to be installed in the container.
HTML conversion uses the ``markdown`` library (pure Python, no system deps).
Jinja2 template rendering uses SandboxedEnvironment to prevent injection attacks.

Typical pipeline
----------------
Markdown template  →  render_jinja2_template  →  md_to_html  →  html_to_pdf
DOCX template      →  fill_docx_template      →  docx_to_pdf
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

# Front-matter delimiter
_FRONT_MATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Jinja2 variable reference: {{ var_name }}  (also {{ obj.attr }})
_JINJA2_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

# Pandoc binary name
_PANDOC_BIN = "pandoc"

# Subprocess timeout (seconds) for pandoc calls
_PANDOC_TIMEOUT = 30

# Regex matching characters that cause "Unicode character not set up for use with LaTeX"
# errors when pandoc uses the default pdflatex engine.  Covers:
#   - Non-BMP supplementary planes (U+10000–U+10FFFF): most emoji
#   - BMP Miscellaneous Symbols (U+2600–U+26FF): ☀ ☁ ✓ ✗ …
#   - BMP Dingbats (U+2700–U+27BF): ✅ (U+2705), ❌ (U+274C), ➤ …
#   - BMP Misc Symbols and Arrows (U+2B00–U+2BFF): ⭐ (U+2B50) …
#   - Variation Selectors (U+FE00–U+FE0F): emoji presentation modifiers
_LATEX_UNSAFE_RE = re.compile(
    r"[\U00010000-\U0010FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F]"
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def parse_md_front_matter(md_text: str) -> tuple[dict, str]:
    """Extract YAML front-matter from a Markdown string.

    Returns a tuple of ``(metadata_dict, body_without_front_matter)``.
    If no front-matter block is present, returns ``({}, md_text)``.

    Only scalar values (str, int, float, bool) and simple lists are
    supported — complex nested structures are returned as raw strings.
    """
    match = _FRONT_MATTER_RE.match(md_text)
    if not match:
        return {}, md_text

    raw_yaml = match.group(1)
    body = md_text[match.end():]

    metadata: dict = {}
    for line in raw_yaml.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        metadata[key] = value

    return metadata, body


def extract_template_variables(template_text: str) -> list[str]:
    """Return sorted unique Jinja2 variable names found in *template_text*.

    Only simple ``{{ variable }}`` references are extracted — Jinja2 control
    structures (``{% … %}``) and filters (``{{ x | filter }}``) are ignored.
    The reserved ``content`` variable is always included in the result.
    """
    _, body = parse_md_front_matter(template_text)
    found = set(_JINJA2_VAR_RE.findall(body))
    found.add("content")
    return sorted(found)


def strip_non_bmp(text: str) -> str:
    """Remove characters that cause LaTeX errors when pandoc generates PDF.

    Strips non-BMP supplementary-plane characters (U+10000+, e.g. 📊) *and*
    BMP symbol/emoji blocks (e.g. ✅ U+2705, ⭐ U+2B50) that the default
    pdflatex engine cannot typeset without special font packages.
    """
    return _LATEX_UNSAFE_RE.sub("", text)


def render_jinja2_template(template_text: str, variables: dict) -> str:
    """Render *template_text* as a Jinja2 template with *variables*.

    Uses ``SandboxedEnvironment`` to prevent arbitrary code execution.

    Parameters
    ----------
    template_text:
        Jinja2 template string (may include ``{{ var }}``, ``{% for … %}``, etc.).
    variables:
        Mapping of variable names to values passed to the template.

    Returns
    -------
    str
        Rendered output.

    Raises
    ------
    jinja2.TemplateError
        If the template is syntactically invalid or rendering fails.
    """
    from jinja2.sandbox import SandboxedEnvironment

    env = SandboxedEnvironment(autoescape=False)
    tmpl = env.from_string(template_text)
    return tmpl.render(**variables)


def md_to_html(md_text: str, css: Optional[str] = None) -> str:
    """Convert Markdown to an HTML string.

    The ``markdown`` library (pure Python) handles conversion.
    Supported extensions: ``tables``, ``fenced_code``, ``toc``.

    Parameters
    ----------
    md_text:
        Input Markdown text.
    css:
        Optional CSS string embedded as a ``<style>`` block in the ``<head>``.

    Returns
    -------
    str
        Complete HTML document (``<html><head>…</head><body>…</body></html>``).
    """
    import markdown as md_lib

    extensions = ["tables", "fenced_code", "toc"]
    body_html = md_lib.markdown(md_text, extensions=extensions)

    style_block = f"<style>\n{css}\n</style>\n" if css else ""
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"{style_block}"
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )


async def html_to_pdf(html: str) -> bytes:
    """Convert an HTML string to PDF bytes using pandoc.

    pandoc reads from stdin and writes PDF to stdout (via ``--output=-``).
    Non-BMP characters (emoji, supplementary symbols) are stripped before
    conversion to avoid LaTeX encoding errors.

    Parameters
    ----------
    html:
        Complete HTML document string.

    Returns
    -------
    bytes
        Raw PDF bytes.

    Raises
    ------
    FileNotFoundError
        If pandoc is not installed.
    RuntimeError
        If pandoc exits with a non-zero status.
    """
    cmd = [_PANDOC_BIN, "--from=html", "--to=pdf", "--output=-"]
    return await _run_pandoc(cmd, input_data=strip_non_bmp(html).encode("utf-8"))


async def html_to_docx(html: str) -> bytes:
    """Convert an HTML string to DOCX bytes using pandoc.

    Parameters
    ----------
    html:
        Complete HTML document string.

    Returns
    -------
    bytes
        Raw DOCX bytes (Open XML format).

    Raises
    ------
    FileNotFoundError
        If pandoc is not installed.
    RuntimeError
        If pandoc exits with a non-zero status.
    """
    cmd = [_PANDOC_BIN, "--from=html", "--to=docx", "--output=-"]
    return await _run_pandoc(cmd, input_data=html.encode("utf-8"))


def fill_docx_template(docx_bytes: bytes, variables: dict) -> bytes:
    """Fill a DOCX template by replacing ``{{variable}}`` placeholders.

    Uses ``python-docx`` to iterate all paragraphs and table cells and
    perform find-replace on their text content.  Placeholders use the
    ``{{key}}`` syntax (double curly braces, no spaces).

    Parameters
    ----------
    docx_bytes:
        Raw DOCX bytes of the template file.
    variables:
        Mapping of placeholder names to replacement values.

    Returns
    -------
    bytes
        Modified DOCX bytes with placeholders replaced.

    Notes
    -----
    - Complex paragraph formatting (e.g. mixed bold/italic within a run
      that spans a placeholder boundary) may be partially flattened.
    - Jinja2 constructs (``{% if … %}``) are NOT supported in DOCX templates.
    """
    from docx import Document

    doc = Document(io.BytesIO(docx_bytes))
    _replace_in_docx(doc, variables)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def docx_to_pdf(docx_bytes: bytes) -> bytes:
    """Convert DOCX bytes to PDF bytes using pandoc.

    Parameters
    ----------
    docx_bytes:
        Raw DOCX bytes.

    Returns
    -------
    bytes
        Raw PDF bytes.

    Raises
    ------
    FileNotFoundError
        If pandoc is not installed.
    RuntimeError
        If pandoc exits with a non-zero status.
    """
    cmd = [_PANDOC_BIN, "--from=docx", "--to=pdf", "--output=-"]
    return await _run_pandoc(cmd, input_data=docx_bytes)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _replace_in_docx(doc: "Document", variables: dict) -> None:  # type: ignore[name-defined]
    """Replace ``{{key}}`` placeholders in all paragraphs and table cells."""
    for para in doc.paragraphs:
        _replace_in_paragraph(para, variables)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _replace_in_paragraph(para, variables)


def _replace_in_paragraph(para: "Paragraph", variables: dict) -> None:  # type: ignore[name-defined]
    """Replace placeholders in a single paragraph's runs.

    To handle placeholders that may be split across runs, we reconstruct
    the full paragraph text, perform replacements, then write back.
    """
    full_text = "".join(run.text for run in para.runs)
    if "{{" not in full_text:
        return

    new_text = full_text
    for key, value in variables.items():
        new_text = new_text.replace("{{" + key + "}}", str(value))

    if new_text == full_text:
        return

    # Write the entire new text into the first run, clear the rest
    if para.runs:
        para.runs[0].text = new_text
        for run in para.runs[1:]:
            run.text = ""


async def _run_pandoc(cmd: list[str], input_data: bytes) -> bytes:
    """Run a pandoc subprocess asynchronously, feeding *input_data* via stdin.

    Returns stdout bytes on success.

    Raises
    ------
    FileNotFoundError
        If the pandoc binary is not found.
    RuntimeError
        If pandoc exits with a non-zero return code.
    asyncio.TimeoutError
        If the process exceeds ``_PANDOC_TIMEOUT`` seconds.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise FileNotFoundError(
            f"pandoc binary not found. Ensure pandoc is installed (cmd: {cmd[0]!r})."
        )

    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=input_data),
        timeout=_PANDOC_TIMEOUT,
    )

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"pandoc exited with code {proc.returncode}: {err_msg}"
        )

    log.debug("pandoc: cmd=%r produced %d bytes", cmd, len(stdout))
    return stdout
