"""gSage AI — Binary database helpers.

Provides path resolution for GeoIP and other binary databases mounted via
the ``dbs/`` Docker volume at ``/app/dbs``.

Usage in a custom tool::

    from src.shared.dbs import get_db_path, db_available

    if db_available("geoip", "GeoLite2-City.mmdb"):
        db_path = get_db_path("geoip", "GeoLite2-City.mmdb")
        reader = geoip2.database.Reader(db_path)
    else:
        return ToolResult.failure("DB_UNAVAILABLE", "GeoIP database not found", ...)

The base path is controlled by the ``GSAGE_DBS_PATH`` environment variable
(default: ``/app/dbs``).
"""

from __future__ import annotations

import os
from pathlib import Path


def _base() -> Path:
    """Return the configured dbs root directory."""
    from src.shared.config.settings import get_settings
    return Path(get_settings().gsage_dbs_path)


def get_db_path(category: str, filename: str) -> str:
    """
    Return the absolute path to a binary database file.

    Args:
        category: Sub-directory name (e.g. ``"geoip"``, ``"ip2location"``).
        filename: File name (e.g. ``"GeoLite2-City.mmdb"``).

    Returns:
        Absolute path string.

    Raises:
        FileNotFoundError: If the file does not exist at the resolved path.
    """
    path = _base() / category / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Database file not found: {path}. "
            f"Run dbs/{category}/update.sh to download it."
        )
    return str(path)


def db_available(category: str, filename: str) -> bool:
    """
    Return True if a binary database file exists and is readable.

    Args:
        category: Sub-directory name (e.g. ``"geoip"``).
        filename: File name (e.g. ``"GeoLite2-ASN.mmdb"``).

    Returns:
        True if the file exists and is readable.
    """
    path = _base() / category / filename
    return path.is_file() and os.access(path, os.R_OK)
