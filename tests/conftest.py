"""gSage AI — shared test fixtures (Sprint 5.3).

All integration tests use an in-process FastAPI test client (httpx + ASGI
transport) with the real application factory.  External dependencies (DB,
Redis, Elasticsearch) are replaced with lightweight mocks via FastAPI's
``dependency_overrides`` mechanism so tests run without any infrastructure.

Exported constants
------------------
ORG_A, ORG_B   — deterministic UUID objects for two isolated organisations
USER_A, USER_B — deterministic UUID objects for each org's primary member

Helper functions
----------------
make_jwt(user_id, org_id, role)  — produce a signed JWT identical to those
    issued by the real auth routes (uses the same create_access_token utility)
"""

from __future__ import annotations

import uuid
import warnings
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from authlib.deprecate import AuthlibDeprecationWarning

# weaviate-client still triggers this third-party deprecation during import.
warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)

from src.backend_api.app.core.tenant import permissions_for_role
from src.backend_api.app.main import create_app
from src.shared.database import get_db
from src.shared.security.auth import create_access_token

# ---------------------------------------------------------------------------
# Stable test identities
# ---------------------------------------------------------------------------

ORG_A: uuid.UUID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
ORG_B: uuid.UUID = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
USER_A: uuid.UUID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000011")
USER_B: uuid.UUID = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000022")


# ---------------------------------------------------------------------------
# JWT factory
# ---------------------------------------------------------------------------


def make_jwt(user_id: uuid.UUID, org_id: uuid.UUID, role: str = "member") -> str:
    """Return a signed JWT for *user_id* scoped to *org_id*.

    Uses the same ``create_access_token`` called by the real auth routes,
    so the token validates correctly through ``decode_token``.
    """
    return create_access_token(
        {
            "sub": str(user_id),
            "org_id": str(org_id),
            "org_role": role,
            "permissions": permissions_for_role(role),
            "email": f"{role}@test.gsage.dev",
            "type": "access",
        }
    )


# ---------------------------------------------------------------------------
# Async mock DB session
# ---------------------------------------------------------------------------


def _make_mock_db() -> AsyncMock:
    """Return an ``AsyncSession`` mock that returns empty query results.

    The mock supports the async context‑manager protocol used by ``get_db``.
    Individual tests can customise return values by overriding
    ``mock.execute.return_value``.
    """
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=None)
    scalar_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    mock = AsyncMock()
    mock.execute = AsyncMock(return_value=scalar_result)
    mock.add = MagicMock()
    mock.flush = AsyncMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.refresh = AsyncMock()
    return mock


async def _mock_get_db() -> AsyncGenerator[AsyncMock, None]:
    """Async generator that yields a mock ``AsyncSession``."""
    yield _make_mock_db()


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    """Shared FastAPI application instance for the whole test session.

    ``get_db`` is replaced globally so no PostgreSQL connection is attempted.
    Tests that need custom DB behaviour can override ``get_db`` locally using
    ``app.dependency_overrides``.
    """
    _app = create_app()
    _app.dependency_overrides[get_db] = _mock_get_db
    return _app


# ---------------------------------------------------------------------------
# HTTP client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    """Unauthenticated async HTTP client."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.fixture
async def client_a(app) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client pre-authenticated as a member of Org A."""
    token = make_jwt(USER_A, ORG_A)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def client_b(app) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client pre-authenticated as a member of Org B."""
    token = make_jwt(USER_B, ORG_B)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac
