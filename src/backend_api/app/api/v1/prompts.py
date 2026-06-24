"""gSage AI — Prompt Library routes.

Routes
------
GET    /orgs/{org_id}/prompts/categories          List category tree
POST   /orgs/{org_id}/prompts/categories          Create category
PUT    /orgs/{org_id}/prompts/categories/{id}     Update category
DELETE /orgs/{org_id}/prompts/categories/{id}     Delete category
GET    /orgs/{org_id}/prompts                     List prompts (with filters)
POST   /orgs/{org_id}/prompts/search              Search prompts (ILIKE)
POST   /orgs/{org_id}/prompts                     Create prompt
POST   /orgs/{org_id}/prompts/{id}/favorite       Toggle favorite
GET    /orgs/{org_id}/prompts/favorites           List user favorites
GET    /orgs/{org_id}/prompts/{id}                Get single prompt
PUT    /orgs/{org_id}/prompts/{id}                Update prompt
DELETE /orgs/{org_id}/prompts/{id}                Delete prompt
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.backend_api.app.api.deps import get_current_user, get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.prompts import (
    PromptCategoryCreate,
    PromptCategoryOut,
    PromptCategoryUpdate,
    PromptCreate,
    PromptListResponse,
    PromptOut,
    PromptSearchRequest,
    PromptUpdate,
)
from src.shared.database import get_db
from src.shared.models.prompt import (
    GSagePrompt,
    GSagePromptCategory,
    GSageUserPromptFavorite,
)
from src.shared.models.user import GSageUser

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_category_tree(
    categories: list[GSagePromptCategory],
    parent_id: uuid.UUID | None = None,
) -> list[PromptCategoryOut]:
    """Build a nested category tree from a flat list."""
    result: list[PromptCategoryOut] = []
    for cat in categories:
        if cat.parent_id == parent_id:
            node = PromptCategoryOut(
                id=cat.id,
                name=cat.name,
                parent_id=cat.parent_id,
                dept_id=cat.dept_id,
                description=cat.description,
                sort_order=cat.sort_order,
                is_active=cat.is_active,
                children=_build_category_tree(categories, cat.id),
                prompt_count=len(cat.prompts) if cat.prompts else 0,
                created_at=cat.created_at,
                updated_at=cat.updated_at,
            )
            result.append(node)
    return result


def _category_visibility_filter(
    org_id: uuid.UUID,
    dept_id: uuid.UUID | None,
) -> Any:  # ColumnElement[bool]
    """Return a WHERE clause for categories visible to the given scope."""
    return and_(
        GSagePromptCategory.org_id == org_id,
        GSagePromptCategory.is_active == True,  # noqa: E712
        or_(
            GSagePromptCategory.dept_id.is_(None),
            GSagePromptCategory.dept_id == dept_id,
        ),
    )


def _prompt_visibility_filter(
    org_id: uuid.UUID,
    dept_id: uuid.UUID | None,
    user_id: uuid.UUID,
) -> Any:  # ColumnElement[bool]
    """Return a WHERE clause for prompts visible to the given user/scope."""
    return and_(
        GSagePrompt.org_id == org_id,
        GSagePrompt.is_active == True,  # noqa: E712
        or_(
            GSagePrompt.scope == "organization",
            and_(
                GSagePrompt.scope == "department",
                GSagePrompt.dept_id == dept_id,
            ),
            and_(
                GSagePrompt.scope == "personal",
                GSagePrompt.created_by == user_id,
            ),
        ),
    )


def _prompt_to_out(prompt: GSagePrompt, is_favorite: bool = False) -> PromptOut:
    """Convert a GSagePrompt ORM object to a PromptOut schema."""
    return PromptOut(
        id=prompt.id,
        title=prompt.title,
        description=prompt.description,
        content=prompt.content,
        scope=prompt.scope,
        category_id=prompt.category_id,
        category_name=prompt.category.name if prompt.category else None,
        created_by=prompt.created_by,
        creator_name=prompt.creator.full_name if prompt.creator else "",
        is_favorite=is_favorite,
        usage_count=prompt.usage_count,
        created_at=prompt.created_at,
        updated_at=prompt.updated_at,
    )


# ---------------------------------------------------------------------------
# Category routes
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/prompts/categories",
    response_model=list[PromptCategoryOut],
    summary="List prompt category tree",
)
async def list_categories(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PromptCategoryOut]:
    """Return the full category tree for the org (org-level + user's dept)."""
    ctx.require_permission("prompts:read")

    result = await db.execute(
        select(GSagePromptCategory)
        .options(selectinload(GSagePromptCategory.prompts))
        .where(_category_visibility_filter(org_id, ctx.dept_id))
        .order_by(GSagePromptCategory.sort_order, GSagePromptCategory.name)
    )
    categories = result.scalars().all()
    return _build_category_tree(list(categories))


@router.post(
    "/orgs/{org_id}/prompts/categories",
    response_model=PromptCategoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a prompt category",
)
async def create_category(
    org_id: uuid.UUID,
    payload: PromptCategoryCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PromptCategoryOut:
    """Create a new prompt category (org-level or department-level)."""
    ctx.require_permission("prompts:manage")

    dept_id = payload.dept_id or None

    # Only org admins (admin:access) can create org-level categories
    if dept_id is None and not ctx.has_permission("admin:access"):
        raise HTTPException(
            status_code=403,
            detail="Only organization admins can create org-level categories",
        )

    category = GSagePromptCategory(
        org_id=org_id,
        dept_id=dept_id,
        parent_id=payload.parent_id,
        name=payload.name,
        description=payload.description,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)

    return PromptCategoryOut(
        id=category.id,
        name=category.name,
        parent_id=category.parent_id,
        dept_id=category.dept_id,
        description=category.description,
        sort_order=category.sort_order,
        is_active=category.is_active,
        children=[],
        prompt_count=0,
        created_at=category.created_at,
        updated_at=category.updated_at,
    )


@router.put(
    "/orgs/{org_id}/prompts/categories/{category_id}",
    response_model=PromptCategoryOut,
    summary="Update a prompt category",
)
async def update_category(
    org_id: uuid.UUID,
    category_id: uuid.UUID,
    payload: PromptCategoryUpdate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PromptCategoryOut:
    """Update a prompt category."""
    ctx.require_permission("prompts:manage")

    result = await db.execute(
        select(GSagePromptCategory)
        .options(selectinload(GSagePromptCategory.prompts))
        .options(selectinload(GSagePromptCategory.children))
        .where(
            and_(
                GSagePromptCategory.id == category_id,
                GSagePromptCategory.org_id == org_id,
            )
        )
    )
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")

    update_data = payload.model_dump(exclude_unset=True)

    # Only org admins can set dept_id to NULL (org-level)
    new_dept_id = update_data.get("dept_id")
    if new_dept_id is None and "dept_id" in update_data:
        if not ctx.has_permission("admin:access"):
            raise HTTPException(
                status_code=403,
                detail="Only organization admins can set org-level scope",
            )

    for key, value in update_data.items():
        setattr(category, key, value)

    await db.commit()
    await db.refresh(category)

    return PromptCategoryOut(
        id=category.id,
        name=category.name,
        parent_id=category.parent_id,
        dept_id=category.dept_id,
        description=category.description,
        sort_order=category.sort_order,
        is_active=category.is_active,
        children=[],
        prompt_count=len(category.prompts) if category.prompts else 0,
        created_at=category.created_at,
        updated_at=category.updated_at,
    )


@router.delete(
    "/orgs/{org_id}/prompts/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a prompt category",
)
async def delete_category(
    org_id: uuid.UUID,
    category_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a prompt category. Only allowed if empty (no prompts, no children)."""
    ctx.require_permission("prompts:manage")

    result = await db.execute(
        select(GSagePromptCategory)
        .options(
            selectinload(GSagePromptCategory.prompts),
            selectinload(GSagePromptCategory.children),
        )
        .where(
            and_(
                GSagePromptCategory.id == category_id,
                GSagePromptCategory.org_id == org_id,
            )
        )
    )
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")

    # Check if category has prompts
    if category.prompts:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete category: it contains {len(category.prompts)} prompt(s). Remove or reassign them first.",
        )

    # Check if category has children
    if category.children:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete category: it contains {len(category.children)} subcategory(ies). Delete them first.",
        )

    await db.delete(category)
    await db.commit()


# ---------------------------------------------------------------------------
# Prompt list + search routes
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/prompts",
    response_model=PromptListResponse,
    summary="List prompts (with filters)",
)
async def list_prompts(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: str | None = None,
    category_id: uuid.UUID | None = None,
    favorites_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> PromptListResponse:
    """List prompts visible to the current user, with optional filters."""
    ctx.require_permission("prompts:read")

    visibility = _prompt_visibility_filter(org_id, ctx.dept_id, ctx.user_id)

    conditions = [visibility]
    if scope:
        conditions.append(GSagePrompt.scope == scope)
    if category_id:
        conditions.append(GSagePrompt.category_id == category_id)

    # Count total
    count_stmt = (
        select(func.count())
        .select_from(GSagePrompt)
        .where(and_(*conditions))
    )
    total = (await db.execute(count_stmt)).scalar_one()

    # Fetch page
    stmt = (
        select(GSagePrompt).options(selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator))
        .where(and_(*conditions))
        .order_by(GSagePrompt.usage_count.desc(), GSagePrompt.title)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    prompts = result.scalars().all()

    # Resolve favorites for this user
    fav_stmt = select(GSageUserPromptFavorite.prompt_id).where(
        GSageUserPromptFavorite.user_id == ctx.user_id,
        GSageUserPromptFavorite.prompt_id.in_([p.id for p in prompts]),
    )
    fav_result = await db.execute(fav_stmt)
    fav_ids = {row[0] for row in fav_result.fetchall()}

    # Filter by favorites_only
    if favorites_only:
        prompts = [p for p in prompts if p.id in fav_ids]
        total = len(prompts)

    return PromptListResponse(
        prompts=[_prompt_to_out(p, p.id in fav_ids) for p in prompts],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/orgs/{org_id}/prompts/search",
    response_model=PromptListResponse,
    summary="Search prompts (ILIKE)",
)
async def search_prompts(
    org_id: uuid.UUID,
    payload: PromptSearchRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PromptListResponse:
    """Full-text search over title, description, and content using ILIKE."""
    ctx.require_permission("prompts:read")

    visibility = _prompt_visibility_filter(org_id, ctx.dept_id, ctx.user_id)
    conditions = [visibility]

    if payload.scope:
        conditions.append(GSagePrompt.scope == payload.scope)
    if payload.category_id:
        conditions.append(GSagePrompt.category_id == payload.category_id)

    if payload.query:
        term = f"%{payload.query.strip()}%"
        conditions.append(
            or_(
                GSagePrompt.title.ilike(term),
                GSagePrompt.description.ilike(term),
                GSagePrompt.content.ilike(term),
            )
        )

    # Count total
    count_stmt = (
        select(func.count())
        .select_from(GSagePrompt)
        .where(and_(*conditions))
    )
    total = (await db.execute(count_stmt)).scalar_one()

    # Fetch page
    stmt = (
        select(GSagePrompt).options(selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator))
        .where(and_(*conditions))
        .order_by(GSagePrompt.usage_count.desc(), GSagePrompt.title)
        .offset((payload.page - 1) * payload.page_size)
        .limit(payload.page_size)
    )
    result = await db.execute(stmt)
    prompts = result.scalars().all()

    # Resolve favorites
    fav_stmt = select(GSageUserPromptFavorite.prompt_id).where(
        GSageUserPromptFavorite.user_id == ctx.user_id,
        GSageUserPromptFavorite.prompt_id.in_([p.id for p in prompts]),
    )
    fav_result = await db.execute(fav_stmt)
    fav_ids = {row[0] for row in fav_result.fetchall()}

    if payload.favorites_only:
        prompts = [p for p in prompts if p.id in fav_ids]
        total = len(prompts)

    return PromptListResponse(
        prompts=[_prompt_to_out(p, p.id in fav_ids) for p in prompts],
        total=total,
        page=payload.page,
        page_size=payload.page_size,
    )


# ---------------------------------------------------------------------------
# Create prompt
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/prompts",
    response_model=PromptOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a prompt",
)
async def create_prompt(
    org_id: uuid.UUID,
    payload: PromptCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[GSageUser, Depends(get_current_user)],
) -> PromptOut:
    """Create a new prompt."""
    ctx.require_permission("prompts:write")

    # Validate scope permissions
    if payload.scope == "organization" and not ctx.has_permission("prompts:manage"):
        raise HTTPException(
            status_code=403,
            detail="Only admins can create organization-scoped prompts",
        )
    if payload.scope == "department" and not ctx.has_permission("prompts:manage"):
        raise HTTPException(
            status_code=403,
            detail="Only admins can create department-scoped prompts",
        )

    prompt = GSagePrompt(
        org_id=org_id,
        dept_id=ctx.dept_id if payload.scope == "department" else None,
        created_by=user.id,
        category_id=payload.category_id,
        title=payload.title,
        content=payload.content,
        description=payload.description,
        scope=payload.scope,
    )
    db.add(prompt)
    await db.commit()

    # Re-query with eager-loaded relationships to avoid MissingGreenlet
    result = await db.execute(
        select(GSagePrompt).options(
            selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator)
        ).where(GSagePrompt.id == prompt.id)
    )
    prompt = result.scalar_one()

    return _prompt_to_out(prompt)


# ---------------------------------------------------------------------------
# Favorite routes (BEFORE /{prompt_id} to avoid path conflicts)
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/prompts/{prompt_id}/favorite",
    response_model=dict,
    summary="Toggle prompt favorite",
)
async def toggle_favorite(
    org_id: uuid.UUID,
    prompt_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Toggle the favorite status of a prompt for the current user."""
    ctx.require_permission("prompts:read")

    visibility = _prompt_visibility_filter(org_id, ctx.dept_id, ctx.user_id)
    result = await db.execute(
        select(GSagePrompt.id).where(
            and_(GSagePrompt.id == prompt_id, visibility)
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Prompt not found")

    existing = await db.get(GSageUserPromptFavorite, (ctx.user_id, prompt_id))

    if existing:
        await db.delete(existing)
        await db.commit()
        return {"favorited": False}
    else:
        fav = GSageUserPromptFavorite(user_id=ctx.user_id, prompt_id=prompt_id)
        db.add(fav)
        await db.commit()
        return {"favorited": True}


@router.get(
    "/orgs/{org_id}/prompts/favorites",
    response_model=PromptListResponse,
    summary="List user's favorite prompts",
)
async def list_favorites(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = 1,
    page_size: int = 20,
) -> PromptListResponse:
    """List prompts favorited by the current user."""
    ctx.require_permission("prompts:read")

    visibility = _prompt_visibility_filter(org_id, ctx.dept_id, ctx.user_id)

    count_stmt = (
        select(func.count())
        .select_from(GSagePrompt)
        .join(
            GSageUserPromptFavorite,
            and_(
                GSageUserPromptFavorite.prompt_id == GSagePrompt.id,
                GSageUserPromptFavorite.user_id == ctx.user_id,
            ),
        )
        .where(visibility)
    )
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = (
        select(GSagePrompt).options(selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator))
        .join(
            GSageUserPromptFavorite,
            and_(
                GSageUserPromptFavorite.prompt_id == GSagePrompt.id,
                GSageUserPromptFavorite.user_id == ctx.user_id,
            ),
        )
        .where(visibility)
        .order_by(GSagePrompt.usage_count.desc(), GSagePrompt.title)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    prompts = result.scalars().all()

    return PromptListResponse(
        prompts=[_prompt_to_out(p, is_favorite=True) for p in prompts],
        total=total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# Single-prompt routes (/{prompt_id} — AFTER /favorites)
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/prompts/{prompt_id}",
    response_model=PromptOut,
    summary="Get a single prompt",
)
async def get_prompt(
    org_id: uuid.UUID,
    prompt_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PromptOut:
    """Get a single prompt by ID."""
    ctx.require_permission("prompts:read")

    visibility = _prompt_visibility_filter(org_id, ctx.dept_id, ctx.user_id)

    result = await db.execute(
        select(GSagePrompt).options(selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator)).where(
            and_(GSagePrompt.id == prompt_id, visibility)
        )
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")

    fav = await db.get(GSageUserPromptFavorite, (ctx.user_id, prompt_id))
    return _prompt_to_out(prompt, fav is not None)


@router.put(
    "/orgs/{org_id}/prompts/{prompt_id}",
    response_model=PromptOut,
    summary="Update a prompt",
)
async def update_prompt(
    org_id: uuid.UUID,
    prompt_id: uuid.UUID,
    payload: PromptUpdate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PromptOut:
    """Update a prompt. Only the creator or an admin can update."""
    ctx.require_permission("prompts:write")

    result = await db.execute(
        select(GSagePrompt).options(selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator)).where(
            and_(GSagePrompt.id == prompt_id, GSagePrompt.org_id == org_id)
        )
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if prompt.created_by != ctx.user_id and not ctx.has_permission("prompts:manage"):
        raise HTTPException(status_code=403, detail="You can only edit your own prompts")

    update_data = payload.model_dump(exclude_unset=True)

    new_scope = update_data.get("scope")
    if new_scope and new_scope != "personal" and not ctx.has_permission("prompts:manage"):
        raise HTTPException(
            status_code=403,
            detail="Only admins can set department or organization scope",
        )

    for key, value in update_data.items():
        setattr(prompt, key, value)

    await db.commit()

    # Re-query with eager-loaded relationships to avoid MissingGreenlet
    result = await db.execute(
        select(GSagePrompt).options(
            selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator)
        ).where(GSagePrompt.id == prompt.id)
    )
    prompt = result.scalar_one()

    fav = await db.get(GSageUserPromptFavorite, (ctx.user_id, prompt_id))
    return _prompt_to_out(prompt, fav is not None)


@router.delete(
    "/orgs/{org_id}/prompts/{prompt_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a prompt",
)
async def delete_prompt(
    org_id: uuid.UUID,
    prompt_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Delete a prompt. Only the creator or an admin can delete."""
    ctx.require_permission("prompts:write")

    result = await db.execute(
        select(GSagePrompt).options(selectinload(GSagePrompt.category), selectinload(GSagePrompt.creator)).where(
            and_(GSagePrompt.id == prompt_id, GSagePrompt.org_id == org_id)
        )
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if prompt.created_by != ctx.user_id and not ctx.has_permission("prompts:manage"):
        raise HTTPException(status_code=403, detail="You can only delete your own prompts")

    await db.delete(prompt)
    await db.commit()
