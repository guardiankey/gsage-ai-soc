"""gSage AI — Async database engine and session factory."""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_engine = None
_engine_loop_id: int | None = None
_async_session_maker: async_sessionmaker | None = None


def _get_engine():
    """Return (or create) the async SQLAlchemy engine.

    In Celery workers (fork pool) each ``asyncio.run()`` creates a new event
    loop.  asyncpg connections are bound to the loop they were created on, so
    a cached engine whose pool holds connections from a defunct loop will fail
    with *"Future attached to a different loop"*.

    This function detects the situation and transparently recreates the engine
    when the running loop has changed.
    """
    global _engine, _engine_loop_id, _async_session_maker

    # Detect stale engine: connections from a previous event loop are unusable.
    try:
        current_loop_id: int | None = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = None

    if (
        _engine is not None
        and current_loop_id is not None
        and _engine_loop_id is not None
        and _engine_loop_id != current_loop_id
    ):
        # Abandon pool connections WITHOUT async close — the old event loop is
        # already closed, so scheduling coroutines on it would raise
        # "Event loop is closed".  dispose(close=False) drops pool references
        # without touching the dead connections.
        try:
            _engine.sync_engine.dispose(close=False)
        except Exception:
            pass
        _engine = None
        _async_session_maker = None

    if _engine is None:
        from src.shared.config.settings import get_settings
        settings = get_settings()

        # asyncpg supports ``server_settings`` via SQLAlchemy's
        # ``connect_args``.  Use it to enforce server-side statement
        # and idle-in-transaction timeouts so a cancelled / leaked
        # session cannot keep a Postgres backend in
        # "idle in transaction" forever.
        server_settings: dict[str, str] = {}
        if settings.database_statement_timeout_ms > 0:
            server_settings["statement_timeout"] = str(
                settings.database_statement_timeout_ms
            )
        if settings.database_idle_in_tx_timeout_ms > 0:
            server_settings["idle_in_transaction_session_timeout"] = str(
                settings.database_idle_in_tx_timeout_ms
            )

        connect_args: dict = {}
        if server_settings:
            connect_args["server_settings"] = server_settings

        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_recycle=settings.database_pool_recycle_seconds,
            connect_args=connect_args,
        )
        _engine_loop_id = current_loop_id

    return _engine


def _get_session_maker() -> async_sessionmaker:
    global _async_session_maker
    # Always call _get_engine() first: it detects stale engines (loop change in
    # Celery fork workers) and resets _async_session_maker to None when needed.
    engine = _get_engine()
    if _async_session_maker is None:
        _async_session_maker = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_maker


async def dispose_engine_pool() -> None:
    """Dispose the shared async engine's connection pool.

    Call inside a Celery ``asyncio.run()`` *before* the event loop closes so
    all asyncpg connections are properly closed.  Without this, asyncpg tries
    to close connections during GC after the loop is shut down and logs noisy
    "Event loop is closed" / "Future attached to a different loop" errors.
    """
    global _engine, _async_session_maker
    if _engine is not None:
        try:
            await _engine.dispose()
        except Exception:
            pass
        _engine = None
        _async_session_maker = None


def create_pooled_engine(settings_override=None):
    """Create a fresh async engine with project-standard pool settings.

    Use this in Celery tasks, workers, or any context that needs its own
    engine instance (e.g. ``asyncio.run()`` creates a fresh event loop) but
    must still respect the configured pool size, overflow, and timeouts.

    Parameters
    ----------
    settings_override : Settings | None
        Optional pre-fetched settings object.  If ``None`` (the default),
        ``get_settings()`` is called once internally.
    """
    from src.shared.config.settings import get_settings

    s = settings_override or get_settings()

    # Server-side safety nets: enforce statement & idle-in-transaction
    # timeouts on every connection so a cancelled / leaked session cannot
    # keep a Postgres backend in "idle in transaction" forever.
    server_settings: dict[str, str] = {}
    if s.database_statement_timeout_ms > 0:
        server_settings["statement_timeout"] = str(s.database_statement_timeout_ms)
    if s.database_idle_in_tx_timeout_ms > 0:
        server_settings["idle_in_transaction_session_timeout"] = str(
            s.database_idle_in_tx_timeout_ms
        )

    connect_args: dict = {}
    if server_settings:
        connect_args["server_settings"] = server_settings

    return create_async_engine(
        s.database_url,
        pool_pre_ping=True,
        pool_size=s.database_pool_size,
        max_overflow=s.database_max_overflow,
        pool_recycle=s.database_pool_recycle_seconds,
        connect_args=connect_args,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield an async database session per request."""
    session_maker = _get_session_maker()
    async with session_maker() as session:
        yield session
