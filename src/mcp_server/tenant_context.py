"""gSage AI — MCP Server tenant context from HTTP headers.

Uses ``contextvars`` to make tenant identity available deep inside MCP
handlers without passing them through every function parameter.

The :class:`TenantHeadersMiddleware` extracts ``X-Organization-ID``,
``X-User-ID`` and ``X-Org-Role`` from every incoming Starlette request
and stores them in a :class:`TenantHeaders` instance.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context variable — set per-request by middleware, read by MCP handlers
# ---------------------------------------------------------------------------

_tenant_var: contextvars.ContextVar[Optional["TenantHeaders"]] = contextvars.ContextVar(
    "mcp_tenant_headers", default=None
)


@dataclass(frozen=True)
class TenantHeaders:
    """Immutable snapshot of tenant identity extracted from HTTP headers."""

    org_id: uuid.UUID
    user_id: uuid.UUID
    org_role: str
    interface: str = "web"
    # gSage session (conversation) ID forwarded by the backend so background
    # tasks can be scoped to the originating conversation.
    gsage_session_id: Optional[uuid.UUID] = None
    # Active department forwarded by the backend (set when user has selected a dept).
    dept_id: Optional[uuid.UUID] = None


def get_tenant_headers() -> TenantHeaders:
    """Return the current request's tenant headers.

    Raises:
        RuntimeError: If called outside a request with tenant headers.
    """
    headers = _tenant_var.get()
    if headers is None:
        raise RuntimeError("No tenant headers in current context — is TenantHeadersMiddleware active?")
    return headers


def get_tenant_headers_or_none() -> Optional[TenantHeaders]:
    """Return the current tenant headers, or ``None`` if unavailable."""
    return _tenant_var.get()


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------


class TenantHeadersMiddleware(BaseHTTPMiddleware):
    """Extract tenant identity headers and populate the context variable.

    Expected headers (set by agno MCPTools ``header_provider``):
    - ``X-Organization-ID`` — UUID of the tenant org
    - ``X-User-ID``         — UUID of the requesting user
    - ``X-Org-Role``        — role string (owner, admin, member, viewer, apikey)
    - ``X-Interface``       — access interface (web, email, telegram, whatsapp, slack, api, cli)
    """

    _VALID_INTERFACES = frozenset({"web", "email", "telegram", "whatsapp", "slack", "api", "cli"})

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        org_id_raw = request.headers.get("X-Organization-ID")
        user_id_raw = request.headers.get("X-User-ID")
        org_role = request.headers.get("X-Org-Role", "member")
        interface_raw = request.headers.get("X-Interface", "web").lower()
        interface = interface_raw if interface_raw in self._VALID_INTERFACES else "web"
        session_id_raw = request.headers.get("X-gSage-Session-ID")

        if org_id_raw and user_id_raw:
            try:
                gsage_session_id = uuid.UUID(session_id_raw) if session_id_raw else None
                dept_id_raw = request.headers.get("X-Department-Id")
                dept_id: uuid.UUID | None = None
                if dept_id_raw:
                    try:
                        dept_id = uuid.UUID(dept_id_raw)
                    except ValueError:
                        pass  # Malformed dept UUID — ignore, don't block request
                tenant = TenantHeaders(
                    org_id=uuid.UUID(org_id_raw),
                    user_id=uuid.UUID(user_id_raw),
                    org_role=org_role,
                    interface=interface,
                    gsage_session_id=gsage_session_id,
                    dept_id=dept_id,
                )
                _tenant_var.set(tenant)
                log.debug(
                    "MCP tenant headers: interface_raw=%s interface=%s org=%s user=%s session=%s dept=%s",
                    interface_raw,
                    interface,
                    org_id_raw,
                    user_id_raw,
                    session_id_raw,
                    dept_id_raw,
                )
            except ValueError:
                pass  # Invalid UUIDs — headers are ignored, handlers will deny access

        response = await call_next(request)
        return response
