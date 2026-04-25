"""Sprint 5.3 — Cross-tenant isolation tests.

These tests verify that multi-tenant isolation is enforced at the
auth and route levels — *without* a real database.

Important technical details
----------------------------
FastAPI's ``get_tenant_context`` dependency validates the JWT's ``org_id``
against the ``{org_id}`` path parameter **before** any database call:

    if token_org_id != org_id:
        raise HTTPException(403, "Token org_id does not match route organization")

Because this check happens in-process (no DB round-trip), these tests do not
need a running PostgreSQL instance.  The ``get_db`` dependency is replaced
with a mock by the shared ``app`` fixture in ``conftest.py``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import ORG_A, ORG_B, USER_A, USER_B
from src.backend_api.app.core.tenant import TenantContext


# ---------------------------------------------------------------------------
# 1. JWT cross-org rejection (auth-layer isolation)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_org_b_jwt_rejected_on_org_a_conversations(client_b: AsyncClient):
    """Org B's JWT is rejected with 403 when accessing Org A's conversations."""
    resp = await client_b.get(f"/api/v1/orgs/{ORG_A}/chat/conversations")
    assert resp.status_code == 403, resp.text
    detail = resp.json().get("detail", "")
    assert "org" in detail.lower() or "organization" in detail.lower()


@pytest.mark.integration
async def test_org_b_jwt_rejected_on_org_a_sessions(client_b: AsyncClient):
    """Org B's JWT is rejected with 403 when accessing Org A's sessions."""
    resp = await client_b.get(f"/api/v1/orgs/{ORG_A}/sessions")
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_org_a_jwt_rejected_on_org_b_conversations(client_a: AsyncClient):
    """Org A's JWT is rejected with 403 when accessing Org B's conversations."""
    resp = await client_a.get(f"/api/v1/orgs/{ORG_B}/chat/conversations")
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_org_a_jwt_rejected_on_org_b_agents(client_a: AsyncClient):
    """Org A's JWT cannot list Org B's agents."""
    resp = await client_a.get(f"/api/v1/orgs/{ORG_B}/agents")
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_org_a_jwt_rejected_on_org_b_knowledge(client_a: AsyncClient):
    """Org A's JWT cannot access Org B's knowledge base."""
    resp = await client_a.get(f"/api/v1/orgs/{ORG_B}/knowledge/content")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 2. Unauthenticated requests are rejected
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unauthenticated_conversations_rejected(client: AsyncClient):
    resp = await client.get(f"/api/v1/orgs/{ORG_A}/chat/conversations")
    assert resp.status_code == 401, resp.text


@pytest.mark.integration
async def test_unauthenticated_sessions_rejected(client: AsyncClient):
    resp = await client.get(f"/api/v1/orgs/{ORG_A}/sessions")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 3. Agno session prefix isolation (unit — no HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_id_prefix_is_org_scoped():
    """TenantContext.build_session_id produces namespaced session IDs."""
    ctx_a = TenantContext(
        user_id=USER_A, org_id=ORG_A, org_role="member", permissions=[]
    )
    ctx_b = TenantContext(
        user_id=USER_B, org_id=ORG_B, org_role="member", permissions=[]
    )

    sid_a = ctx_a.build_session_id("user", str(USER_A))
    sid_b = ctx_b.build_session_id("user", str(USER_B))

    assert sid_a.startswith(f"org_{ORG_A}:")
    assert sid_b.startswith(f"org_{ORG_B}:")
    assert sid_a != sid_b


@pytest.mark.unit
def test_session_prefix_differs_per_org():
    """Two orgs with the same user identifier produce different session IDs."""
    shared_uid = USER_A
    ctx_a = TenantContext(
        user_id=shared_uid, org_id=ORG_A, org_role="member", permissions=[]
    )
    ctx_b = TenantContext(
        user_id=shared_uid, org_id=ORG_B, org_role="member", permissions=[]
    )

    assert ctx_a.agno_session_prefix != ctx_b.agno_session_prefix
    assert ctx_a.build_session_id("u", "x") != ctx_b.build_session_id("u", "x")


# ---------------------------------------------------------------------------
# 4. Permission isolation (unit — no HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_viewer_cannot_write_knowledge():
    ctx = TenantContext(
        user_id=USER_A,
        org_id=ORG_A,
        org_role="viewer",
        permissions=["agents:read", "sessions:read", "knowledge:read"],
    )
    assert not ctx.has_permission("knowledge:write")


@pytest.mark.unit
def test_member_can_resolve_approvals():
    from src.backend_api.app.core.tenant import permissions_for_role

    perms = permissions_for_role("member")
    ctx = TenantContext(
        user_id=USER_A, org_id=ORG_A, org_role="member", permissions=perms
    )
    assert ctx.has_permission("approvals:resolve")


@pytest.mark.unit
def test_admin_can_resolve_approvals():
    from src.backend_api.app.core.tenant import permissions_for_role

    perms = permissions_for_role("admin")
    ctx = TenantContext(
        user_id=USER_A, org_id=ORG_A, org_role="admin", permissions=perms
    )
    assert ctx.has_permission("approvals:resolve")
