"""Shared constants and helpers for file-oriented tools.

Used by ``read_file``, ``write_file``, and ``change_file_scope`` to avoid
duplication across the file-tool family.  Follows the same pattern as
``csv_shared.py`` (csv tools) and ``_url_utils.py`` (threat-intel tools).
"""

from __future__ import annotations

from src.shared.config.settings import get_settings as _get_settings

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
# Hard cap on bytes for read/display operations (read_file).  Kept small so
# an agent read can never pull an oversized payload into memory/context.
MAX_FILE_BYTES: int = 5 * 1024 * 1024

# Cap for file-manipulation operations (write_file create/edit/append/diff/
# copy/insert_file).  Mirrors the global storage limit
# (settings.file_max_size_bytes / FILE_MAX_SIZE_BYTES, default 1 GB) so the
# tools never reject what the storage layer can hold.  Manipulation output
# is not returned inline to the agent — the agent receives file metadata
# plus a bounded preview — so this cap does not drive token consumption.
MAX_EDIT_FILE_BYTES: int = _get_settings().file_max_size_bytes


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
