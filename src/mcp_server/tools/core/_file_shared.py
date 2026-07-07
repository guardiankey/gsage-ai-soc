"""Shared constants and helpers for file-oriented tools.

Used by ``read_file``, ``write_file``, and ``change_file_scope`` to avoid
duplication across the file-tool family.  Follows the same pattern as
``csv_shared.py`` (csv tools) and ``_url_utils.py`` (threat-intel tools).
"""

from __future__ import annotations

# ── Text MIME prefixes ─────────────────────────────────────────────────────
# Content types that can be decoded to UTF-8 and treated as text.
# Kept in sync with GSageFile.content_type values produced by _store_file.
TEXT_MIME_PREFIXES: tuple[str, ...] = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/x-sh",
    "application/x-python",
    "application/csv",
)

# ── Size limits ────────────────────────────────────────────────────────────
# Hard cap on bytes for read/write operations (5 MB).
MAX_FILE_BYTES: int = 5 * 1024 * 1024


# ── Text detection ─────────────────────────────────────────────────────────

def is_text_content(content_type: str) -> bool:
    """Return True if *content_type* is a supported text format."""
    ct = content_type.lower().split(";")[0].strip()
    return any(ct.startswith(p) for p in TEXT_MIME_PREFIXES)


# ── Content-type inference from file extension ─────────────────────────────

EXT_TO_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".xml": "application/xml",
    ".py": "text/x-python",
    ".js": "application/javascript",
    ".ts": "application/typescript",
    ".css": "text/css",
    ".sh": "application/x-sh",
    ".log": "text/plain",
    ".diff": "text/plain",
    ".patch": "text/plain",
    ".rst": "text/x-rst",
    ".tex": "text/x-tex",
}


def infer_content_type(filename: str) -> str:
    """Infer MIME type from file extension. Falls back to ``text/plain``."""
    import os

    ext = os.path.splitext(filename)[1].lower()
    return EXT_TO_MIME.get(ext, "text/plain")
