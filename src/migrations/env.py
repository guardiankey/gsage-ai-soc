"""Alembic environment — gSage AI."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import all models so SQLAlchemy metadata is fully populated before autogenerate.
# The order matters: base first, then models with FK dependencies.
import src.shared.models  # noqa: F401 — registers all mappers

from src.shared.models.base import Base
from src.shared.config.settings import get_settings

# Alembic Config object (gives access to values in alembic.ini)
config = context.config

# Configure Python logging from the ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the database URL from application settings (ignores the placeholder in alembic.ini)
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (outputs SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
