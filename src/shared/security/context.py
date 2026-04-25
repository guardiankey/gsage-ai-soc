"""gSage AI — AgentContext (mandatory for all executions)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RequestSource(str, Enum):
    """Origin of the request."""

    WEB = "web"
    EMAIL = "email"
    CLI = "cli"
    API = "api"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    SLACK = "slack"


@dataclass(frozen=True)
class AgentContext:
    """
    Immutable context object passed to every tool, agent, and audit log.
    
    MUST be constructed from authenticated session/API key at request entry point.
    NEVER trust fields from user input — always resolve from server-side session/token.
    
    Fields:
        org_id: Current organization ID
        user_id: Current user ID
        group_ids: User's group IDs
        permissions: Resolved permission tags (e.g., ["dns:read", "whois:read"])
        api_key_id: API key ID if request came via API (optional)
        request_id: Trace ID for correlating logs (UUID)
        source: Origin of the request (web, email, cli, api)
    
    Usage:
        - Permission checks (tool execution)
        - Audit logging
        - Tenant isolation (org_id filtering)
        - Rate limiting
        - Cache key generation
    """

    org_id: uuid.UUID
    user_id: uuid.UUID
    group_ids: list[uuid.UUID]
    permissions: list[str]
    request_id: uuid.UUID
    source: RequestSource
    api_key_id: Optional[uuid.UUID] = None
    dept_id: Optional[uuid.UUID] = None

    def __post_init__(self):
        """Validate context fields."""
        if not self.org_id:
            raise ValueError("org_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not isinstance(self.permissions, list):
            raise ValueError("permissions must be a list")
        if not isinstance(self.group_ids, list):
            raise ValueError("group_ids must be a list")
        if not self.request_id:
            raise ValueError("request_id is required")
        if not isinstance(self.source, RequestSource):
            raise ValueError("source must be a RequestSource enum")

    def has_permission(self, permission_tag: str) -> bool:
        """Check if context has a specific permission tag. Wildcard '*' grants all."""
        return "*" in self.permissions or permission_tag in self.permissions

    def has_any_permission(self, *permission_tags: str) -> bool:
        """Check if context has at least one of the specified permission tags. Wildcard '*' grants all."""
        if "*" in self.permissions:
            return True
        return any(tag in self.permissions for tag in permission_tags)

    def has_all_permissions(self, *permission_tags: str) -> bool:
        """Check if context has all specified permission tags. Wildcard '*' grants all."""
        if "*" in self.permissions:
            return True
        return all(tag in self.permissions for tag in permission_tags)

    # ── Serialisation helpers (used by Celery background worker) ────────────

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict for Celery task storage."""
        return {
            "org_id": str(self.org_id),
            "user_id": str(self.user_id),
            "group_ids": [str(g) for g in self.group_ids],
            "permissions": list(self.permissions),
            "request_id": str(self.request_id),
            "source": self.source.value,
            "api_key_id": str(self.api_key_id) if self.api_key_id else None,
            "dept_id": str(self.dept_id) if self.dept_id else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentContext":
        """Reconstruct an AgentContext from a serialised dict."""
        return cls(
            org_id=uuid.UUID(data["org_id"]),
            user_id=uuid.UUID(data["user_id"]),
            group_ids=[uuid.UUID(g) for g in data.get("group_ids", [])],
            permissions=data["permissions"],
            request_id=uuid.UUID(data["request_id"]),
            source=RequestSource(data["source"]),
            api_key_id=uuid.UUID(data["api_key_id"]) if data.get("api_key_id") else None,
            dept_id=uuid.UUID(data["dept_id"]) if data.get("dept_id") else None,
        )
