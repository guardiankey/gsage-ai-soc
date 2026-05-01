"""Curator — admin API endpoints (/a/).

All endpoints require the ``X-API-Key`` header matching ``CURATOR_API_KEY``.

Endpoints:
    GET    /a/list_collections
    POST   /a/create_collection
    PUT    /a/{collection_id}/update_collection
    POST   /a/{collection_id}/add_item
    DELETE /a/{collection_id}/del_item
    GET    /a/{collection_id}/view_item
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .dump import schedule_dump
from .models import Collection, Item, _make_slug
from .schemas import (
    CollectionCreate,
    CollectionOut,
    CollectionUpdate,
    ItemAdd,
    ItemDelete,
    ItemOut,
    PaginatedItems,
)
from .validation import CIDR_TYPES, validate_value

log = logging.getLogger(__name__)

router = APIRouter(prefix="/a", tags=["admin"])


# ── Auth dependency ───────────────────────────────────────────────────────────


async def _verify_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    settings = get_settings()
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")


_auth = Depends(_verify_api_key)


# ── Helper ────────────────────────────────────────────────────────────────────


async def _get_collection_or_404(collection_id: int, session: AsyncSession) -> Collection:
    c = await session.get(Collection, collection_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    return c


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/list_collections", response_model=list[CollectionOut], dependencies=[_auth])
async def list_collections(
    db: AsyncSession = Depends(get_db),
    active_only: bool = Query(False, description="If true, return only active collections"),
) -> list[CollectionOut]:
    count_sq = (
        select(Item.collection_id, func.count(Item.id).label("item_count"))
        .group_by(Item.collection_id)
        .subquery()
    )
    stmt = (
        select(Collection, func.coalesce(count_sq.c.item_count, 0).label("item_count"))
        .outerjoin(count_sq, Collection.id == count_sq.c.collection_id)
        .order_by(Collection.short_description, Collection.subtype, Collection.type)
    )
    if active_only:
        stmt = stmt.where(Collection.active.is_(True))
    result = await db.execute(stmt)
    rows = result.all()
    return [
        CollectionOut.model_validate({**col.__dict__, "item_count": cnt})
        for col, cnt in rows
    ]


@router.post("/create_collection", response_model=CollectionOut, status_code=201, dependencies=[_auth])
async def create_collection(
    payload: CollectionCreate,
    db: AsyncSession = Depends(get_db),
) -> Collection:
    slug = _make_slug(payload.short_description, payload.subtype, payload.type)

    # Check uniqueness
    existing = (await db.execute(select(Collection).where(Collection.slug == slug).limit(1))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Collection with slug '{slug}' already exists (id={existing.id})")

    c = Collection(
        short_description=payload.short_description,
        description=payload.description,
        slug=slug,
        type=payload.type,
        subtype=payload.subtype,
        active=payload.active,
        status="idle",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    log.info("create_collection: id=%s slug=%s", c.id, c.slug)
    return c


@router.put("/{collection_id}/update_collection", response_model=CollectionOut, dependencies=[_auth])
async def update_collection(
    collection_id: int,
    payload: CollectionUpdate,
    db: AsyncSession = Depends(get_db),
) -> Collection:
    c = await _get_collection_or_404(collection_id, db)

    if payload.short_description is not None:
        c.short_description = payload.short_description
        # Regenerate slug when short_description changes
        c.slug = _make_slug(payload.short_description, c.subtype, c.type)
    if payload.description is not None:
        c.description = payload.description
    if payload.active is not None:
        c.active = payload.active

    c.touch()
    await db.commit()
    await db.refresh(c)
    return c


@router.post("/{collection_id}/add_item", response_model=ItemOut, status_code=200, dependencies=[_auth])
async def add_item(
    collection_id: int,
    payload: ItemAdd,
    db: AsyncSession = Depends(get_db),
) -> Item:
    c = await _get_collection_or_404(collection_id, db)

    # Validate value for this collection type
    try:
        canonical = validate_value(c.type, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    now = datetime.now(tz=timezone.utc)
    expire_at = now + timedelta(days=payload.expire_days) if payload.expire_days else None

    use_cidr = c.type in CIDR_TYPES

    # Upsert: look for existing item with same canonical value and type
    if use_cidr:
        stmt = (
            select(Item)
            .where(Item.collection_id == collection_id)
            .where(Item.cidr == canonical)
            .where(Item.type == payload.type)
            .limit(1)
        )
    else:
        stmt = (
            select(Item)
            .where(Item.collection_id == collection_id)
            .where(Item.value == canonical)
            .where(Item.type == payload.type)
            .limit(1)
        )

    existing_item = (await db.execute(stmt)).scalar_one_or_none()

    if existing_item is not None:
        # If the item had been soft-deleted, re-activate it preserving the
        # original created_at (so past differential history is not rewritten).
        # The re_added_at marker yields a '+' event for today's diff.
        if existing_item.deleted_at is not None:
            existing_item.deleted_at = None
            existing_item.re_added_at = now
        else:
            # Active row being refreshed: bump re_added_at to mark a logical re-add.
            existing_item.re_added_at = now
        existing_item.expire_at = expire_at
        if payload.public_reference is not None:
            existing_item.public_reference = payload.public_reference
        if payload.reference is not None:
            existing_item.reference = payload.reference
        item = existing_item
        log.debug("add_item: upserted item id=%s in collection %s", item.id, collection_id)
    else:
        item = Item(
            collection_id=collection_id,
            cidr=canonical if use_cidr else None,
            value=None if use_cidr else canonical,
            public_reference=payload.public_reference,
            reference=payload.reference,
            type=payload.type,
            created_at=now,
            expire_at=expire_at,
        )
        db.add(item)
        log.debug("add_item: new item in collection %s", collection_id)

    # State machine: always commit to DB, then manage status
    prev_status = c.status

    if prev_status == "idle":
        c.status = "waiting"
        c.touch()

    await db.commit()
    await db.refresh(item)

    # Trigger background dump only on idle→waiting transition
    if prev_status == "idle":
        await schedule_dump(collection_id)

    return item


@router.delete("/{collection_id}/del_item", status_code=200, dependencies=[_auth])
async def del_item(
    collection_id: int,
    payload: ItemDelete,
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = await _get_collection_or_404(collection_id, db)

    try:
        canonical = validate_value(c.type, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    use_cidr = c.type in CIDR_TYPES

    if use_cidr:
        stmt = (
            select(Item)
            .where(Item.collection_id == collection_id)
            .where(Item.cidr == canonical)
            .where(Item.type == payload.type)
            .where(Item.deleted_at.is_(None))
        )
    else:
        stmt = (
            select(Item)
            .where(Item.collection_id == collection_id)
            .where(Item.value == canonical)
            .where(Item.type == payload.type)
            .where(Item.deleted_at.is_(None))
        )

    items = (await db.execute(stmt)).scalars().all()
    if not items:
        raise HTTPException(status_code=404, detail="Item not found")

    now = datetime.now(tz=timezone.utc)
    for it in items:
        # Soft-delete: preserves '-' event for today's differential and the
        # _purge_loop physically removes the row after DIFF_RETENTION_DAYS.
        it.deleted_at = now

    prev_status = c.status
    if prev_status == "idle":
        c.status = "waiting"
        c.touch()

    await db.commit()

    if prev_status == "idle":
        await schedule_dump(collection_id)

    return {"deleted": len(items), "value": payload.value}


@router.get("/{collection_id}/view_item", response_model=PaginatedItems, dependencies=[_auth])
async def view_item(
    collection_id: int,
    db: AsyncSession = Depends(get_db),
    value: str | None = Query(None, description="Filter by exact value (or CIDR)"),
    type: str | None = Query(None, description="Filter by item type (blocklist/allowlist/suspected)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
) -> PaginatedItems:
    await _get_collection_or_404(collection_id, db)

    base = select(Item).where(Item.collection_id == collection_id)
    count_base = select(func.count()).select_from(Item).where(Item.collection_id == collection_id)

    if value:
        base = base.where((Item.value == value) | (func.host(Item.cidr) == value))
        count_base = count_base.where((Item.value == value) | (func.host(Item.cidr) == value))
    if type:
        base = base.where(Item.type == type)
        count_base = count_base.where(Item.type == type)

    total = (await db.execute(count_base)).scalar_one()
    items = (
        await db.execute(base.order_by(Item.id).offset((page - 1) * per_page).limit(per_page))
    ).scalars().all()

    return PaginatedItems(
        total=total,
        page=page,
        per_page=per_page,
        items=[ItemOut.model_validate(it) for it in items],
    )
