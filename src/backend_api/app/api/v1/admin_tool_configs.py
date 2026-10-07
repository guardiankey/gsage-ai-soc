"""gSage AI — Admin: Tool configuration endpoints.

Routes (prefix: /v1/orgs/{org_id}/admin):
    GET    /tool-configs                    List tool configurations
    POST   /tool-configs                    Create a tool configuration
    GET    /tool-configs/{config_id}        Get configuration detail (decrypted)
    PATCH  /tool-configs/{config_id}        Update configuration
    DELETE /tool-configs/{config_id}        Delete configuration
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.admin import (
    ToolCatalogEntry,
    ToolConfigCreate,
    ToolConfigOut,
    ToolConfigSummary,
    ToolConfigUpdate,
    ToolMetadataOut,
    ToolSettingsUpdate,
)
from src.backend_api.app.services import tool_auto_approve
from src.shared.cache.permissions_cache import get_perm_redis_client
from src.shared.cache.tool_config_cache import invalidate_tool_config_cache
from src.shared.config.settings import get_settings
from src.shared.models.department import GSageDepartment
from src.shared.models.org_tool_settings import GSageOrgToolSettings
from src.shared.models.tool import GSageTool
from src.shared.models.tool_config import GSageToolConfig, GSageToolConfigDepartment
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()


async def _invalidate_config_caches(org_id: uuid.UUID) -> None:
    """Best-effort flush of every cached tool config for an organization."""
    try:
        await invalidate_tool_config_cache(get_perm_redis_client(), org_id)
    except Exception:  # pragma: no cover — best-effort
        pass
    # Clear the backend's in-memory auto_approve cache so the edited values
    # take effect immediately instead of after the 30s TTL.
    tool_auto_approve.invalidate_cache(org_id=org_id)


async def _validate_dept_ids(
    db: AsyncSession, org_id: uuid.UUID, dept_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """De-duplicate ``dept_ids`` and ensure every id belongs to ``org_id``."""
    unique = list(dict.fromkeys(dept_ids))
    if not unique:
        return []
    found = set(
        (
            await db.execute(
                select(GSageDepartment.id).where(
                    GSageDepartment.org_id == org_id,
                    GSageDepartment.id.in_(unique),
                )
            )
        )
        .scalars()
        .all()
    )
    missing = [str(d) for d in unique if d not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown department id(s) for this organization: {', '.join(missing)}",
        )
    return unique


async def _replace_departments(
    db: AsyncSession, tc: GSageToolConfig, dept_ids: list[uuid.UUID]
) -> None:
    """Replace the config's department scope with ``dept_ids`` (empty = org-wide).

    Uses the ORM ``departments`` collection so the ``delete-orphan`` cascade
    removes the previous rows; the collection is always loaded (``lazy="selectin"``
    on fetch, empty for a newly created config).  The intermediate flush emits
    the DELETEs before the new INSERTs, so re-scoping to an overlapping set
    (e.g. ``{A, B}`` → ``{B, C}``) cannot trip the ``(org, tool, profile, dept)``
    unique constraint.
    """
    tc.departments.clear()
    await db.flush()
    for dept_id in dept_ids:
        tc.departments.append(
            GSageToolConfigDepartment(
                org_id=tc.org_id,
                tool_name=tc.tool_name,
                profile_id=tc.profile_id,
                dept_id=dept_id,
            )
        )


async def _load_dept_ids(db: AsyncSession, config_id: uuid.UUID) -> list[uuid.UUID]:
    """Return the department ids scoped to a config (empty = org-wide)."""
    rows = (
        await db.execute(
            select(GSageToolConfigDepartment.dept_id).where(
                GSageToolConfigDepartment.tool_config_id == config_id
            )
        )
    ).scalars().all()
    return list(rows)


async def _check_scope_conflicts(
    db: AsyncSession,
    org_id: uuid.UUID,
    tool_name: str,
    profile_id: str,
    scope: str,
    dept_ids: list[uuid.UUID],
    *,
    exclude_config_id: uuid.UUID | None = None,
) -> None:
    """Enforce the scope invariants for ``(org, tool, profile)``.

    * ``scope='org'``  → at most one global config (else 409).
    * ``scope='dept'`` → ``dept_ids`` must be non-empty and none of them may
      already be covered by another config of the same tool/profile (else 409).
    """
    if scope == "org":
        stmt = select(GSageToolConfig.id).where(
            GSageToolConfig.org_id == org_id,
            GSageToolConfig.tool_name == tool_name,
            GSageToolConfig.profile_id == profile_id,
            GSageToolConfig.scope == "org",
        )
        if exclude_config_id is not None:
            stmt = stmt.where(GSageToolConfig.id != exclude_config_id)
        if (await db.execute(stmt)).first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A global (org-wide) config for this (org, tool_name, "
                    "profile_id) already exists"
                ),
            )
        return

    if not dept_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope='dept' requires at least one department.",
        )

    stmt = select(GSageToolConfigDepartment.dept_id).where(
        GSageToolConfigDepartment.org_id == org_id,
        GSageToolConfigDepartment.tool_name == tool_name,
        GSageToolConfigDepartment.profile_id == profile_id,
        GSageToolConfigDepartment.dept_id.in_(dept_ids),
    )
    if exclude_config_id is not None:
        stmt = stmt.where(GSageToolConfigDepartment.tool_config_id != exclude_config_id)
    conflicts = (await db.execute(stmt)).scalars().all()
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Department(s) already covered by another config for this "
                f"tool/profile: {', '.join(str(d) for d in conflicts)}"
            ),
        )


def _tool_config_to_out(
    tc: GSageToolConfig, dept_ids: list[uuid.UUID] | None = None
) -> ToolConfigOut:
    """Convert model to response schema (decrypts config)."""
    if dept_ids is None:
        dept_ids = [d.dept_id for d in tc.departments]
    return ToolConfigOut(
        id=tc.id,
        org_id=tc.org_id,
        scope=tc.scope,
        dept_ids=list(dept_ids),
        tool_name=tc.tool_name,
        profile_id=tc.profile_id,
        description=tc.description,
        config=tc.config,  # property handles decryption
        updated_by_user_id=tc.updated_by_user_id,
        created_at=tc.created_at,
        updated_at=tc.updated_at,
    )


@router.get(
    "/tool-configs",
    response_model=list[ToolConfigOut],
    summary="List tool configurations",
)
async def list_tool_configs(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
    tool_name: str | None = None,
    dept_id: uuid.UUID | None = None,
) -> list[ToolConfigOut]:
    """List all tool configurations for the organization.

    Optional filters: ``tool_name``, ``dept_id`` (returns configs that apply to
    the department — org-wide ones *and* those explicitly scoped to it).
    """
    stmt = (
        select(GSageToolConfig)
        .options(selectinload(GSageToolConfig.departments))
        .where(GSageToolConfig.org_id == org_id)
    )
    if tool_name:
        stmt = stmt.where(GSageToolConfig.tool_name == tool_name)
    if dept_id is not None:
        stmt = stmt.where(
            (GSageToolConfig.scope == "org")
            | (
                (GSageToolConfig.scope == "dept")
                & GSageToolConfig.departments.any(
                    GSageToolConfigDepartment.dept_id == dept_id
                )
            )
        )
    stmt = stmt.order_by(GSageToolConfig.tool_name, GSageToolConfig.profile_id)

    result = await db.execute(stmt)
    return [_tool_config_to_out(tc) for tc in result.scalars().all()]


@router.get(
    "/tools",
    summary="List available tool names (for dropdowns)",
)
async def list_available_tools(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Return distinct tool names from the tool registry for use in
    combobox / select components.
    """
    stmt = (
        select(
            GSageTool.name,
            GSageTool.display_name,
            GSageTool.category,
        )
        .order_by(GSageTool.category, GSageTool.name)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {"name": r.name, "display_name": r.display_name, "category": r.category}
        for r in rows
    ]


@router.post(
    "/tool-configs",
    response_model=ToolConfigOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tool configuration",
)
async def create_tool_config(
    org_id: uuid.UUID,
    payload: ToolConfigCreate,
    ctx: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> ToolConfigOut:
    """Create a new tool configuration.

    ``scope='org'`` creates the org-wide config (at most one per tool/profile).
    ``scope='dept'`` creates a department-scoped config: ``dept_ids`` must be
    non-empty and not already covered by another config of the same
    tool/profile.  Raises 409 on conflicts.
    """
    scope = payload.scope
    dept_ids = (
        await _validate_dept_ids(db, org_id, payload.dept_ids)
        if scope == "dept"
        else []
    )
    await _check_scope_conflicts(
        db, org_id, payload.tool_name, payload.profile_id, scope, dept_ids
    )

    tc = GSageToolConfig(
        org_id=org_id,
        scope=scope,
        tool_name=payload.tool_name,
        profile_id=payload.profile_id,
        description=payload.description,
        updated_by_user_id=ctx.user_id,
    )
    tc.config = payload.config  # encrypts via property setter
    db.add(tc)
    await db.flush()  # assign tc.id before creating the department rows
    if scope == "dept":
        await _replace_departments(db, tc, dept_ids)
    await db.commit()
    await db.refresh(tc)
    # Drop any stale config the MCP server may have cached for this org so
    # the new values take effect immediately instead of after the TTL.
    await _invalidate_config_caches(org_id)
    return _tool_config_to_out(tc, dept_ids=dept_ids)


@router.get(
    "/tool-configs/{config_id}",
    response_model=ToolConfigOut,
    summary="Get tool configuration detail",
)
async def get_tool_config(
    org_id: uuid.UUID,
    config_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> ToolConfigOut:
    result = await db.execute(
        select(GSageToolConfig).where(
            GSageToolConfig.id == config_id,
            GSageToolConfig.org_id == org_id,
        )
    )
    tc = result.scalar_one_or_none()
    if tc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool config not found")
    return _tool_config_to_out(tc)


@router.patch(
    "/tool-configs/{config_id}",
    response_model=ToolConfigOut,
    summary="Update tool configuration",
)
async def update_tool_config(
    org_id: uuid.UUID,
    config_id: uuid.UUID,
    payload: ToolConfigUpdate,
    ctx: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> ToolConfigOut:
    result = await db.execute(
        select(GSageToolConfig).where(
            GSageToolConfig.id == config_id,
            GSageToolConfig.org_id == org_id,
        )
    )
    tc = result.scalar_one_or_none()
    if tc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool config not found")

    original_scope = tc.scope
    new_tool_name = payload.tool_name if payload.tool_name is not None else tc.tool_name
    new_profile_id = payload.profile_id if payload.profile_id is not None else tc.profile_id
    new_scope = payload.scope if payload.scope is not None else original_scope
    identity_changed = (new_tool_name, new_profile_id) != (tc.tool_name, tc.profile_id)

    # Resolve the resulting department set:
    #   scope='org'            → no departments (cleared);
    #   dept_ids provided      → use them (must be non-empty when scope='dept');
    #   otherwise              → keep the current set.
    current_dept_ids = await _load_dept_ids(db, config_id)
    if new_scope == "org":
        new_dept_ids: list[uuid.UUID] = []
    elif payload.dept_ids is not None:
        new_dept_ids = await _validate_dept_ids(db, org_id, payload.dept_ids)
    else:
        new_dept_ids = current_dept_ids

    # Enforce the scope invariants (U1 for 'org', overlap rule for 'dept').
    await _check_scope_conflicts(
        db,
        org_id,
        new_tool_name,
        new_profile_id,
        new_scope,
        new_dept_ids,
        exclude_config_id=config_id,
    )

    if payload.tool_name is not None:
        tc.tool_name = payload.tool_name
    if payload.profile_id is not None:
        tc.profile_id = payload.profile_id
    if payload.scope is not None:
        tc.scope = payload.scope
    if payload.description is not None:
        tc.description = payload.description
    if payload.config is not None:
        tc.config = payload.config  # encrypts via property setter

    tc.updated_by_user_id = ctx.user_id

    if new_scope == "org":
        if current_dept_ids:
            await db.flush()  # persist tool_name/profile_id/scope first
            await _replace_departments(db, tc, [])
    elif set(new_dept_ids) != set(current_dept_ids):
        await db.flush()  # persist tool_name/profile_id before rewriting the rows
        await _replace_departments(db, tc, new_dept_ids)
    elif identity_changed:
        # Keep the denormalized keys on existing department rows in sync.
        await db.flush()
        await db.execute(
            update(GSageToolConfigDepartment)
            .where(GSageToolConfigDepartment.tool_config_id == tc.id)
            .values(tool_name=tc.tool_name, profile_id=tc.profile_id)
        )

    await db.commit()
    await db.refresh(tc)
    # Drop any stale config the MCP server may have cached for this org so
    # the edited values take effect immediately instead of after the TTL.
    await _invalidate_config_caches(org_id)
    return _tool_config_to_out(tc, dept_ids=new_dept_ids)


@router.delete(
    "/tool-configs/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete tool configuration",
)
async def delete_tool_config(
    org_id: uuid.UUID,
    config_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(GSageToolConfig).where(
            GSageToolConfig.id == config_id,
            GSageToolConfig.org_id == org_id,
        )
    )
    tc = result.scalar_one_or_none()
    if tc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool config not found")

    await db.delete(tc)
    await db.commit()
    # Drop any stale config the MCP server may have cached for this org.
    await _invalidate_config_caches(org_id)


# ---------------------------------------------------------------------------
# Tool Catalog (v2 — namespace-aware, enable/disable)
# ---------------------------------------------------------------------------


@router.get(
    "/tool-catalog",
    response_model=list[ToolCatalogEntry],
    summary="List tool catalog (tools + namespace entries) with configs and enabled state",
)
async def get_tool_catalog(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> list[ToolCatalogEntry]:
    """Return all active tools and synthetic namespace entries.

    Each entry includes:
    - ``is_namespace`` — True for synthetic namespace rows.
    - ``configs`` — lightweight summaries of existing org tool configs.
    - ``is_enabled`` — per-org enable/disable state.
    """
    from sqlalchemy import text

    # ── Query A: real tools (with their own configs only, not namespace configs) ──
    row_a = await db.execute(
        text("""
            SELECT
                t.name,
                t.display_name,
                t.category,
                t.config_namespace,
                FALSE AS is_namespace,
                COALESCE(json_agg(json_build_object(
                    'id', tc.id, 'profile_id', tc.profile_id,
                    'scope', tc.scope, 'description', tc.description
                )) FILTER (WHERE tc.id IS NOT NULL), '[]') AS configs,
                COALESCE(ots.is_enabled, TRUE) AS is_enabled
            FROM gsage_tools t
            LEFT JOIN gsage_tool_configs tc
                ON tc.org_id = CAST(:org_id AS uuid)
                AND tc.tool_name = t.name
            LEFT JOIN gsage_org_tool_settings ots
                ON ots.org_id = CAST(:org_id AS uuid)
                AND ots.tool_name = t.name
            WHERE t.is_active = TRUE
            GROUP BY t.name, t.display_name, t.category, t.config_namespace, ots.is_enabled
            ORDER BY t.config_namespace NULLS LAST, t.category, t.name
        """),
        {"org_id": str(org_id)},
    )

    # ── Query B: synthetic namespace entries ──
    row_b = await db.execute(
        text("""
            WITH ns AS (
                SELECT DISTINCT config_namespace AS name
                FROM gsage_tools
                WHERE is_active = TRUE AND config_namespace IS NOT NULL
            )
            SELECT
                ns.name,
                ns.name AS display_name,
                CAST(NULL AS varchar) AS category,
                CAST(NULL AS varchar) AS config_namespace,
                TRUE AS is_namespace,
                COALESCE(json_agg(json_build_object(
                    'id', tc.id, 'profile_id', tc.profile_id,
                    'scope', tc.scope, 'description', tc.description
                )) FILTER (WHERE tc.id IS NOT NULL), '[]') AS configs,
                COALESCE(ots.is_enabled, TRUE) AS is_enabled
            FROM ns
            LEFT JOIN gsage_tool_configs tc
                ON tc.org_id = CAST(:org_id AS uuid) AND tc.tool_name = ns.name
            LEFT JOIN gsage_org_tool_settings ots
                ON ots.org_id = CAST(:org_id AS uuid) AND ots.tool_name = ns.name
            GROUP BY ns.name, ots.is_enabled
            ORDER BY ns.name
        """),
        {"org_id": str(org_id)},
    )

    # ── Merge: namespaces first, then tools ──
    entries: list[ToolCatalogEntry] = []
    import json as _json

    def _parse_configs(raw) -> list:
        """Handle asyncpg (list) vs psycopg2 (JSON string) vs COALESCE fallback string."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                return _json.loads(raw)
            except (_json.JSONDecodeError, TypeError):
                return []
        return []

    # ── Query C: department scope per config (from the join table) ──
    rows_c = await db.execute(
        select(
            GSageToolConfigDepartment.tool_config_id,
            GSageToolConfigDepartment.dept_id,
        ).where(GSageToolConfigDepartment.org_id == org_id)
    )
    dept_map: dict[uuid.UUID, list[uuid.UUID]] = {}
    for cfg_id, dept_id in rows_c.all():
        dept_map.setdefault(cfg_id, []).append(dept_id)

    def _summary(c: dict) -> ToolConfigSummary:
        cid = uuid.UUID(str(c["id"]))
        return ToolConfigSummary(
            id=cid,
            profile_id=str(c["profile_id"]),
            scope=str(c.get("scope") or "org"),
            dept_ids=list(dept_map.get(cid, [])),
            description=c.get("description"),
        )

    for r in row_b.mappings().all():
        configs_raw = _parse_configs(r["configs"])
        entries.append(ToolCatalogEntry(
            name=r["name"],
            display_name=r["display_name"],
            category=r["category"],
            config_namespace=r["config_namespace"],
            is_namespace=bool(r["is_namespace"]),
            is_enabled=bool(r["is_enabled"]),
            config_count=len(configs_raw),
            configs=[_summary(c) for c in configs_raw],
        ))

    for r in row_a.mappings().all():
        configs_raw = _parse_configs(r["configs"])
        entries.append(ToolCatalogEntry(
            name=r["name"],
            display_name=r["display_name"],
            category=r["category"],
            config_namespace=r["config_namespace"],
            is_namespace=bool(r["is_namespace"]),
            is_enabled=bool(r["is_enabled"]),
            config_count=len(configs_raw),
            configs=[_summary(c) for c in configs_raw],
        ))

    return entries


@router.patch(
    "/tools/{tool_name:path}/settings",
    summary="Enable or disable a tool/namespace for the org",
)
async def update_tool_settings(
    org_id: uuid.UUID,
    tool_name: str,
    payload: ToolSettingsUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Enable or disable a tool (or namespace) for this organization.

    - ``is_enabled = True`` → DELETE the row (returns to default-enabled state).
    - ``is_enabled = False`` → INSERT or UPDATE the row.
    """
    stmt = select(GSageOrgToolSettings).where(
        GSageOrgToolSettings.org_id == org_id,
        GSageOrgToolSettings.tool_name == tool_name,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if payload.is_enabled:
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        return {"tool_name": tool_name, "is_enabled": True}
    else:
        if existing is None:
            existing = GSageOrgToolSettings(
                org_id=org_id,
                tool_name=tool_name,
                is_enabled=False,
            )
            db.add(existing)
        else:
            existing.is_enabled = False
        await db.commit()
        return {"tool_name": tool_name, "is_enabled": False}


@router.get(
    "/tools/{tool_name:path}",
    response_model=ToolMetadataOut,
    summary="Get tool metadata for documentation modal",
)
async def get_tool_metadata(
    org_id: uuid.UUID,
    tool_name: str,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
) -> ToolMetadataOut:
    """Return full metadata for a single tool from the MCP server registry.

    Proxies to the MCP server's ``GET /tools/{tool_name}/metadata`` endpoint.
    The MCP server reads directly from the in-memory tool registry (Python
    ClassVars) — no DB query needed.
    """
    import httpx

    settings = get_settings()
    mcp_url = getattr(settings, "mcp_server_url", None)
    if not mcp_url:
        raise HTTPException(status_code=502, detail="MCP server URL not configured")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{mcp_url.rstrip('/')}/tools/{tool_name}/metadata",
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
            resp.raise_for_status()
            data = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"MCP server unreachable: {exc}",
        )

    return ToolMetadataOut(**data)
