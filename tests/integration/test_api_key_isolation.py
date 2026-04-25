"""Sprint 5.3 — API key cross-org isolation tests.

Verifies that credentials (JWT or API key) scoped to Org A cannot be used to
access Org B's routes, and that missing credentials are rejected.

These tests operate entirely at the auth layer (before any DB query), using
the real ``get_tenant_context`` dependency wired against a mock database.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import ORG_A, ORG_B, USER_A, make_jwt


# ---------------------------------------------------------------------------
# Cross-org JWT isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_jwt_for_org_a_rejected_on_every_org_b_resource(client_a: AsyncClient):
    """Org A's JWT is rejected on all Org B resource categories."""
    org_b = ORG_B
    routes = [
        f"/api/v1/orgs/{org_b}/chat/conversations",
        f"/api/v1/orgs/{org_b}/sessions",
        f"/api/v1/orgs/{org_b}/agents",
        f"/api/v1/orgs/{org_b}/knowledge/content",
        f"/api/v1/orgs/{org_b}/approvals",
        f"/api/v1/orgs/{org_b}/api-keys",
    ]
    for route in routes:
        resp = await client_a.get(route)
        assert resp.status_code == 403, (
            f"Expected 403 for {route!r}, got {resp.status_code}: {resp.text}"
        )


@pytest.mark.integration
async def test_jwt_for_org_b_rejected_on_every_org_a_resource(client_b: AsyncClient):
    """Org B's JWT is rejected on all Org A resource categories."""
    org_a = ORG_A
    routes = [
        f"/api/v1/orgs/{org_a}/chat/conversations",
        f"/api/v1/orgs/{org_a}/sessions",
        f"/api/v1/orgs/{org_a}/agents",
        f"/api/v1/orgs/{org_a}/knowledge/content",
    ]
    for route in routes:
        resp = await client_b.get(route)
        assert resp.status_code == 403, (
            f"Expected 403 for {route!r}, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Missing / malformed credentials
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_auth_header_returns_401(client: AsyncClient):
    """Requests without any Authorization header are rejected with 401."""
    resp = await client.get(f"/api/v1/orgs/{ORG_A}/chat/conversations")
    assert resp.status_code == 401, resp.text


@pytest.mark.integration
async def test_malformed_jwt_returns_401(client: AsyncClient):
    """A garbled token is rejected with 401."""
    resp = await client.get(
        f"/api/v1/orgs/{ORG_A}/chat/conversations",
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.integration
async def test_empty_bearer_returns_401(client: AsyncClient):
    """An empty Bearer value is rejected with 401."""
    resp = await client.get(
        f"/api/v1/orgs/{ORG_A}/chat/conversations",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Org id mismatch — token has correct user, wrong org
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_token_org_mismatch_is_403_not_404(client: AsyncClient):
    """A JWT with a mismatched org_id gets 403, not 404 or 500."""
    # JWT belongs to a third org that doesn't match either ORG_A or ORG_B
    third_org = uuid.UUID("cccccccc-0000-4000-8000-000000000003")
    token = make_jwt(USER_A, third_org, role="member")

    # Target endpoint for ORG_A
    resp = await client.get(
        f"/api/v1/orgs/{ORG_A}/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# TenantContext.rate_limit_per_minute field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tenant_context_rate_limit_field_defaults_to_none():
    """New TenantContext instances have rate_limit_per_minute = None by default."""
    from src.backend_api.app.core.tenant import TenantContext

    ctx = TenantContext(user_id=USER_A, org_id=ORG_A, org_role="member", permissions=[])
    assert ctx.rate_limit_per_minute is None


@pytest.mark.unit
def test_tenant_context_rate_limit_field_accepts_int():
    """rate_limit_per_minute can be set to a positive integer."""
    from src.backend_api.app.core.tenant import TenantContext

    ctx = TenantContext(
        user_id=USER_A,
        org_id=ORG_A,
        org_role="apikey",
        permissions=[],
        rate_limit_per_minute=120,
    )
    assert ctx.rate_limit_per_minute == 120
