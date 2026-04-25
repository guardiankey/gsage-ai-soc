"""Admin Console — PostgreSQL session helper."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager yielding a PostgreSQL session."""
    from src.shared.database import _get_session_maker  # noqa: PLC0415

    async with _get_session_maker()() as session:
        yield session
