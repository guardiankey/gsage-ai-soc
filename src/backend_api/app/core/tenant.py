"""gSage AI — TenantContext dataclass and permission system.

This module defines:
- ROLE_PERMISSIONS: maps role names to their granted permissions (cumulative).
- TenantContext: resolved auth context for a request scoped to a specific tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Optional

from fastapi import HTTPException, status

# ---------------------------------------------------------------------------
# Permissions per role (cumulative — each role includes all lower-role perms)
# Source: docs/architecture/06-SECURITY-AUTHORIZATION.md
# ---------------------------------------------------------------------------

_VIEWER_PERMISSIONS: frozenset[str] = frozenset(
    [
        "agents:read",
        "sessions:read",
        "knowledge:read",
        "approvals:read",
    ]
)

_MEMBER_PERMISSIONS: frozenset[str] = _VIEWER_PERMISSIONS | frozenset(
    [
        "agents:run",
        "teams:run",
        "memory:read",
        "memory:delete",
        "apikeys:personal",
        "scheduled_jobs:read",
        "approval_rules:read",
        "approvals:resolve",
        "files:upload",
        "files:read",
        "files:delete",
        "datastores:read",
        "network:analyze",
    ]
)

_ADMIN_PERMISSIONS: frozenset[str] = _MEMBER_PERMISSIONS | frozenset(
    [
        "sessions:read:all",
        "sessions:delete",
        "knowledge:write",
        "knowledge:delete",
        "apikeys:manage",
        "org:members",
        "org:api_keys",
        "scheduled_jobs:write",
        "approval_rules:write",
        "files:read:all",
        "files:delete:all",
        "datastores:write",
        "admin:access",
    ]
)

_OWNER_PERMISSIONS: frozenset[str] = _ADMIN_PERMISSIONS | frozenset(
    [
        "org:manage",
    ]
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": _VIEWER_PERMISSIONS,
    "member": _MEMBER_PERMISSIONS,
    "admin": _ADMIN_PERMISSIONS,
    "owner": _OWNER_PERMISSIONS,
}


def permissions_for_role(role: str) -> list[str]:
    """Return the sorted list of permissions for *role*.

    Unknown roles receive an empty list.
    """
    return sorted(ROLE_PERMISSIONS.get(role, frozenset()))


# ---------------------------------------------------------------------------
# TenantContext
# ---------------------------------------------------------------------------


@dataclass
class TenantContext:
    """Resolved authentication context for a request within a specific tenant.

    Produced by ``deps.get_tenant_context`` after validating either a JWT or
    an API key.  All route handlers that need multi-tenant isolation should
    declare this as a dependency rather than ``GSageUser``.
    """

    user_id: uuid.UUID
    org_id: uuid.UUID
    org_role: str
    permissions: list[str] = field(default_factory=list)
    email: Optional[str] = None
    rate_limit_per_minute: Optional[int] = None  # per-key override; None → use global settings
    interface: str = "web"  # access interface: web, email, telegram, whatsapp, slack, api, cli

    # Department context (populated by get_department_context dependency)
    dept_id: Optional[uuid.UUID] = None
    dept_role: Optional[str] = None  # admin | member | viewer (None = org-admin bypass)

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    def has_permission(self, permission: str) -> bool:
        """Return ``True`` if this context grants *permission*.

        Each entry in ``permissions`` is treated as a glob pattern, so
        ``"*"`` matches everything, ``"approvals:*"`` matches any approval
        action, ``"approvals:re?d"`` matches ``"approvals:read"``, etc.
        """
        return any(fnmatch(permission, granted) for granted in self.permissions)

    def require_permission(self, permission: str) -> None:
        """Raise ``HTTP 403`` if this context lacks *permission*.

        Intended for inline checks inside route handlers:

            ctx.require_permission("knowledge:write")
        """
        if not self.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {permission}",
            )

    def require_dept_context(self) -> None:
        """Raise HTTP 400 if no department context is set.

        Routes that operate on department-scoped resources should call this
        at the start of the handler or use ``get_department_context`` dependency.
        """
        if self.dept_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Department context required. Provide X-Department-Id header.",
            )

    def require_dept_admin(self) -> None:
        """Raise HTTP 403 unless the user is a department admin (or org admin/owner)."""
        self.require_dept_context()
        # Org admins bypass dept-level checks
        if self.org_role in ("owner", "admin"):
            return
        if self.dept_role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Department admin role required",
            )

    # ------------------------------------------------------------------
    # Agno session helpers
    # ------------------------------------------------------------------

    @property
    def agno_session_prefix(self) -> str:
        """Tenant-scoped prefix for Agno session IDs.

        Example: ``"org_<org_uuid>"``
        """
        return f"org_{self.org_id}"

    def build_session_id(self, scope: str, identifier: str) -> str:
        """Build a tenant-scoped Agno session ID.

        Args:
            scope: Logical scope (e.g. ``"user"`` or ``"conv"``).
            identifier: Unique identifier within the scope.

        Returns:
            String of the form ``"org_<org_id>:<scope>:<identifier>"``.

        Example::

            ctx.build_session_id("user", "abc123")
            # → "org_550e8400-...:user:abc123"
        """
        return f"{self.agno_session_prefix}:{scope}:{identifier}"
