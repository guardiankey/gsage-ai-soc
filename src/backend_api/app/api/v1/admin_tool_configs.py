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
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.admin import (
    ToolCatalogEntry,
    ToolConfigCreate,
    ToolConfigOut,
    ToolConfigSummary,
    ToolConfigUpdate,
    ToolSettingsUpdate,
)
from src.shared.cache.permissions_cache import get_perm_redis_client
from src.shared.cache.tool_config_cache import invalidate_tool_config_cache
from src.shared.models.org_tool_settings import GSageOrgToolSettings
from src.shared.models.tool import GSageTool
from src.shared.models.tool_config import GSageToolConfig
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()


def _tool_config_to_out(tc: GSageToolConfig) -> ToolConfigOut:
    """Convert model to response schema (decrypts config)."""
    return ToolConfigOut(
        id=tc.id,
        org_id=tc.org_id,
        dept_id=tc.dept_id,
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

    Optional filters: ``tool_name``, ``dept_id``.
    """
    stmt = select(GSageToolConfig).where(GSageToolConfig.org_id == org_id)
    if tool_name:
        stmt = stmt.where(GSageToolConfig.tool_name == tool_name)
    if dept_id is not None:
        stmt = stmt.where(GSageToolConfig.dept_id == dept_id)
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
    """Create a new tool configuration. Raises 409 if the same
    ``(org, dept, tool_name, profile_id)`` already exists.
    """
    clash_stmt = select(GSageToolConfig).where(
        GSageToolConfig.org_id == org_id,
        GSageToolConfig.tool_name == payload.tool_name,
        GSageToolConfig.profile_id == payload.profile_id,
        GSageToolConfig.dept_id == payload.dept_id,
    )
    if (await db.execute(clash_stmt)).scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tool config for this (org, dept, tool_name, profile_id) already exists",
        )

    tc = GSageToolConfig(
        org_id=org_id,
        dept_id=payload.dept_id,
        tool_name=payload.tool_name,
        profile_id=payload.profile_id,
        description=payload.description,
        updated_by_user_id=ctx.user_id,
    )
    tc.config = payload.config  # encrypts via property setter
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    # Drop any stale config the MCP server may have cached for this org so
    # the new values take effect immediately instead of after the TTL.
    await invalidate_tool_config_cache(get_perm_redis_client(), org_id)
    return _tool_config_to_out(tc)


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

    new_tool_name = payload.tool_name if payload.tool_name is not None else tc.tool_name
    new_profile_id = payload.profile_id if payload.profile_id is not None else tc.profile_id
    new_dept_id = payload.dept_id if payload.dept_id is not None else tc.dept_id

    # Check unique constraint only when tool_name, profile_id or dept_id changes
    if (new_tool_name, new_profile_id, new_dept_id) != (tc.tool_name, tc.profile_id, tc.dept_id):
        clash_stmt = select(GSageToolConfig).where(
            and_(
                GSageToolConfig.org_id == org_id,
                GSageToolConfig.id != config_id,
                GSageToolConfig.tool_name == new_tool_name,
                GSageToolConfig.profile_id == new_profile_id,
                GSageToolConfig.dept_id == new_dept_id,
            )
        )
        if (await db.execute(clash_stmt)).scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tool config for this (org, dept, tool_name, profile_id) already exists",
            )

    if payload.tool_name is not None:
        tc.tool_name = payload.tool_name
    if payload.profile_id is not None:
        tc.profile_id = payload.profile_id
    if payload.dept_id is not None:
        tc.dept_id = payload.dept_id
    if payload.description is not None:
        tc.description = payload.description
    if payload.config is not None:
        tc.config = payload.config  # encrypts via property setter

    tc.updated_by_user_id = ctx.user_id
    await db.commit()
    await db.refresh(tc)
    # Drop any stale config the MCP server may have cached for this org so
    # the edited values take effect immediately instead of after the TTL.
    await invalidate_tool_config_cache(get_perm_redis_client(), org_id)
    return _tool_config_to_out(tc)


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
    await invalidate_tool_config_cache(get_perm_redis_client(), org_id)


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
                    'dept_id', tc.dept_id, 'description', tc.description
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
                    'dept_id', tc.dept_id, 'description', tc.description
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

    def _to_uuid(val) -> uuid.UUID | None:
        """Coerce asyncpg UUID / str / None to uuid.UUID."""
        if val is None:
            return None
        if isinstance(val, uuid.UUID):
            return val
        return uuid.UUID(str(val))

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
            configs=[ToolConfigSummary(
                id=uuid.UUID(str(c["id"])),
                profile_id=str(c["profile_id"]),
                dept_id=_to_uuid(c.get("dept_id")),
                description=c.get("description"),
            ) for c in configs_raw],
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
            configs=[ToolConfigSummary(
                id=uuid.UUID(str(c["id"])),
                profile_id=str(c["profile_id"]),
                dept_id=_to_uuid(c.get("dept_id")),
                description=c.get("description"),
            ) for c in configs_raw],
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
