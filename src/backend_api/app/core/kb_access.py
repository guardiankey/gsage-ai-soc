"""gSage AI — Knowledge base scope access control.

Two independent concerns live here:

**Write authorization**
    :func:`allowed_kb_write_scopes` implements the platform scope matrix:

    - ``owner`` / ``admin`` (global) → can write ``user``, ``dept`` and ``org``
    - ``apikey`` (org-level API key) → keeps the legacy flat behaviour: a key
      holding ``knowledge:write`` in ``scoped_permissions`` may write any
      scope; keys without it cannot write at all
    - department ``admin`` (active department context) → ``user`` + ``dept``
    - ``member`` → own ``user`` scope only
    - ``viewer`` (and any unknown role) → no write access

**Read visibility**
    :func:`is_kb_chunk_visible` / :func:`job_visible_for_ctx` decide whether a
    knowledge chunk (or ingest job) is visible to the caller.  Chunk scope
    lives inside the ``meta_data`` JSON payload (agno stores it as a single
    TEXT property in Weaviate); ingest jobs carry dedicated ``scope`` /
    ``user_id`` / ``dept_id`` columns.

    Visibility rules: ``org`` documents are visible to every org member,
    ``dept`` documents only when the caller's active department matches,
    ``user`` documents only to their owner.  Org admins/owners keep oversight
    access to every scope (same precedent as ``sessions:read:all`` /
    ``files:read:all``).

    Chunks without scope metadata are treated as ``org`` (legacy uploads and
    system-seeded knowledge).
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from fastapi import HTTPException, status

from src.backend_api.app.core.tenant import TenantContext

# ---------------------------------------------------------------------------
# Write authorization
# ---------------------------------------------------------------------------

WRITE_SCOPES_FULL: frozenset[str] = frozenset({"user", "dept", "org"})
WRITE_SCOPES_DEPT: frozenset[str] = frozenset({"user", "dept"})
WRITE_SCOPES_USER: frozenset[str] = frozenset({"user"})


def allowed_kb_write_scopes(ctx: TenantContext) -> set[str]:
    """Return the knowledge write scopes *ctx* is allowed to use."""
    if ctx.org_role in ("owner", "admin"):
        return set(WRITE_SCOPES_FULL)
    if ctx.org_role == "apikey":
        # Org-level API keys carry explicit ``scoped_permissions``.  Preserve
        # the legacy flat check: a key holding ``knowledge:write`` may write
        # any scope; keys without it cannot write at all.
        return set(WRITE_SCOPES_FULL) if ctx.has_permission("knowledge:write") else set()
    if ctx.dept_role == "admin":
        return set(WRITE_SCOPES_DEPT)
    if ctx.org_role == "member":
        return set(WRITE_SCOPES_USER)
    return set()


def require_kb_write_scope(ctx: TenantContext, scope: str) -> None:
    """Raise HTTP 403 when *scope* is not among the caller's allowed scopes."""
    allowed = allowed_kb_write_scopes(ctx)
    if scope not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Write access to knowledge scope '{scope}' is not allowed for "
                f"your role. Allowed scopes: {', '.join(sorted(allowed)) or 'none'}."
            ),
        )


# ---------------------------------------------------------------------------
# Read visibility
# ---------------------------------------------------------------------------


def _meta_dict(meta: object) -> dict[str, Any]:
    """Normalise a chunk ``meta_data`` payload (dict, JSON string or None)."""
    if isinstance(meta, Mapping):
        return dict(meta)
    if isinstance(meta, str):
        try:
            parsed = json.loads(meta)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def chunk_scope(meta: object) -> Optional[str]:
    """Return the normalised scope stored in a chunk meta payload (or None)."""
    scope = _meta_dict(meta).get("scope")
    return str(scope).lower() if scope else None


def is_kb_chunk_visible(
    meta: object,
    *,
    user_id: Optional[object],
    dept_id: Optional[object],
    org_admin: bool = False,
) -> bool:
    """Return ``True`` when a chunk is visible to the described caller."""
    if org_admin:
        return True

    data = _meta_dict(meta)
    scope = str(data.get("scope") or "org").lower()

    if scope == "org":
        return True
    if scope == "dept":
        if dept_id is None:
            return False
        return str(data.get("dept_id") or "") == str(dept_id)
    if scope == "user":
        if user_id is None:
            return False
        return str(data.get("user_id") or "") == str(user_id)

    # Unknown scope → treat as org-wide (legacy/system content).
    return True


def chunk_visible_for_ctx(ctx: TenantContext, meta: object) -> bool:
    """Chunk visibility for a resolved :class:`TenantContext`."""
    return is_kb_chunk_visible(
        meta,
        user_id=ctx.user_id,
        dept_id=ctx.dept_id,
        org_admin=ctx.org_role in ("owner", "admin"),
    )


def job_visible_for_ctx(ctx: TenantContext, job: object) -> bool:
    """Ingest-job visibility for a resolved :class:`TenantContext`.

    Uses the job's dedicated ``scope`` / ``user_id`` / ``dept_id`` columns.
    """
    if ctx.org_role in ("owner", "admin"):
        return True

    scope = str(getattr(job, "scope", None) or "org").lower()
    if scope == "org":
        return True
    if scope == "dept":
        job_dept = getattr(job, "dept_id", None)
        return (
            ctx.dept_id is not None
            and job_dept is not None
            and str(job_dept) == str(ctx.dept_id)
        )
    if scope == "user":
        job_user = getattr(job, "user_id", None)
        return job_user is not None and str(job_user) == str(ctx.user_id)

    # Unknown scope → treat as org-wide (legacy rows).
    return True
