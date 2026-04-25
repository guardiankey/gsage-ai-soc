"""Admin Console — configuration wrapper.

Supports overriding the .env file via ``--env /path/to/.env``
before the first Settings() instantiation.
"""

from __future__ import annotations

import os
from pathlib import Path


def configure_env(env_file: str | None = None) -> None:
    """Load a custom .env file into os.environ before Settings is created.

    Must be called BEFORE any ``get_settings()`` / ``get_admin_settings()``
    invocation.  Clears the lru_cache so the Settings singleton picks up the
    new values.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import]

        path = Path(env_file) if env_file else Path(".env")
        if path.exists():
            load_dotenv(str(path), override=True)
    except ImportError:
        # dotenv not installed — fall back to raw os.environ reads
        pass

    # Clear cached settings instance so the new env is used
    try:
        from src.shared.config.settings import get_settings  # noqa: PLC0415

        get_settings.cache_clear()
    except Exception:
        pass


def get_admin_settings():
    """Return the global Settings instance (cached after first call)."""
    from src.shared.config.settings import get_settings  # noqa: PLC0415

    return get_settings()
