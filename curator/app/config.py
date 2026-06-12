"""Curator — application settings loaded from environment variables.

All settings are prefixed with ``CURATOR_`` in the environment.

Required variables:
    CURATOR_DATABASE_URI  — async PostgreSQL connection string (asyncpg driver)
    CURATOR_API_KEY       — secret key required by all /a/ admin endpoints

Optional variables:
    CURATOR_WAIT_TIME     — seconds to accumulate changes before dumping files (default: 10)
    CURATOR_EXPIRY_CHECK_INTERVAL — seconds between expired-item cleanup runs (default: 3600)
    CURATOR_DATA_DIR      — directory where list files are written (default: /data)
    CURATOR_STRICT_VALIDATION — when true, enforce strict per-type value validation
                            (reject malformed domains/urls/emails/hashes/...). Default false:
                            values are accepted as-is (only whitespace-trimmed) to allow
                            URL/email fragments common in reputation lists. ip/cidr types are
                            always validated regardless, since they are stored in a native
                            PostgreSQL CIDR column.

Module-level constants (intentionally not env-driven):
    DIFF_RETENTION_DAYS   — soft-deleted items younger than this are kept so daily/monthly
                            differential files can still be (re)computed; older rows are
                            physically purged. Differential listings expose only the last
                            DIFF_RETENTION_DAYS days.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Hardcoded — adjust here if a longer history is ever needed.
DIFF_RETENTION_DAYS = 30


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CURATOR_", case_sensitive=False)

    database_uri: str = "postgresql+asyncpg://curator:curator@postgres:5432/curator"
    api_key: str = "CHANGE-ME"
    wait_time: int = 10
    expiry_check_interval: int = 3600
    data_dir: str = "/data"
    strict_validation: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
