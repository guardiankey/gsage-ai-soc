"""gSage AI — built-in document templates.

This package ships ready-to-use document templates that ``generate_document``
can use when no ``template_id`` is supplied (or when the caller explicitly
selects a built-in by passing ``template_id="builtin:<name>"``).

Two built-ins are currently provided:

- ``default``   — minimal Markdown template with YAML front-matter and a
  ``{{ content }}`` body. Used for ``md`` / ``html`` / ``docx`` / ``pdf``
  output via the standard Markdown pipeline.
- ``pandoc_gsage`` — full Pandoc/LaTeX bundle (``defaults.yaml`` +
  ``gsage.latex``) producing a branded PDF cover-page document. Used when
  ``pandoc=true`` is requested with ``output_format=pdf``.

Resources are loaded via :mod:`importlib.resources` so they ship inside the
installed Python package and work both in development and in containers.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Final

__all__ = [
    "BUILTIN_DEFAULT_MD",
    "BUILTIN_PANDOC_GSAGE",
    "get_builtin_template_bytes",
    "get_builtin_pandoc_bundle_dir",
]

BUILTIN_DEFAULT_MD: Final[str] = "default"
BUILTIN_PANDOC_GSAGE: Final[str] = "pandoc_gsage"


def get_builtin_template_bytes(name: str) -> bytes:
    """Return the raw bytes of a single-file built-in template.

    Parameters
    ----------
    name:
        Built-in template name. Currently supported: ``"default"``.

    Raises
    ------
    KeyError
        If *name* is not a known single-file built-in.
    """
    if name == BUILTIN_DEFAULT_MD:
        return resources.files(__package__).joinpath("default.md").read_bytes()
    raise KeyError(f"Unknown built-in template: {name!r}")


def get_builtin_pandoc_bundle_dir() -> Path:
    """Return the on-disk directory of the ``pandoc_gsage`` bundle.

    The bundle is shipped as a regular sub-package directory so pandoc can
    read its files directly (``defaults.yaml``, ``gsage.latex``, …) without
    having to extract them to a temporary location.
    """
    pkg = resources.files(__package__).joinpath("pandoc_gsage")
    # ``files()`` returns a Traversable; for a real on-disk package this is a
    # ``MultiplexedPath`` or ``PosixPath``. ``str(...)`` yields the filesystem
    # path which is what we need to pass to pandoc as ``--resource-path`` and
    # as the cwd of the subprocess.
    return Path(str(pkg))
