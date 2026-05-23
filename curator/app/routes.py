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
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, or_, select
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
    published_only: bool = Query(
        False,
        description="If true, return only published collections (HTTP-exposed)",
    ),
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
    if published_only:
        stmt = stmt.where(Collection.published.is_(True))
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
        published=payload.published,
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
    if payload.published is not None:
        c.published = payload.published

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
    created_from: str | None = Query(
        None,
        description="Filter items created at or after this date. Accepts ISO 8601 (with TZ) or YYYY-MM-DD (interpreted as UTC).",
    ),
    created_to: str | None = Query(
        None,
        description="Filter items created at or before this date. Same format as created_from.",
    ),
    expire_from: str | None = Query(
        None,
        description="Filter items whose expire_at is on or after this date. Items with NULL expire_at are excluded unless never_expires=true is also set.",
    ),
    expire_to: str | None = Query(
        None,
        description="Filter items whose expire_at is on or before this date. Items with NULL expire_at are excluded unless never_expires=true is also set.",
    ),
    created_within_days: int | None = Query(
        None,
        ge=1,
        description="Shortcut: keep items created within the last N days (relative to now, UTC). Mutually exclusive with created_from/to (ANDed if both supplied).",
    ),
    expires_within_days: int | None = Query(
        None,
        ge=1,
        description="Shortcut: keep items that will expire within the next N days (from now, UTC). Excludes never-expiring entries.",
    ),
    never_expires: bool | None = Query(
        None,
        description="If true, return only items with NULL expire_at. If false, exclude them. If omitted, no filter.",
    ),
    expired_only: bool = Query(
        False,
        description="If true, return only items whose expire_at is in the past (already expired but not yet pruned).",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
) -> PaginatedItems:
    await _get_collection_or_404(collection_id, db)

    # ── Parse date inputs ─────────────────────────────────────────────────────
    created_from_dt = _parse_date_filter("created_from", created_from)
    created_to_dt = _parse_date_filter("created_to", created_to)
    expire_from_dt = _parse_date_filter("expire_from", expire_from)
    expire_to_dt = _parse_date_filter("expire_to", expire_to)

    now = datetime.now(timezone.utc)
    if created_within_days is not None:
        rel_from = now - timedelta(days=created_within_days)
        created_from_dt = max(created_from_dt, rel_from) if created_from_dt else rel_from
    if expires_within_days is not None:
        rel_to = now + timedelta(days=expires_within_days)
        expire_to_dt = min(expire_to_dt, rel_to) if expire_to_dt else rel_to
        # "expires within N days" implies the item DOES expire — force
        # never_expires=false unless caller explicitly overrode.
        if never_expires is None:
            never_expires = False

    base = select(Item).where(Item.collection_id == collection_id)
    count_base = select(func.count()).select_from(Item).where(Item.collection_id == collection_id)

    def _apply(stmt):
        if value:
            stmt = stmt.where((Item.value == value) | (func.host(Item.cidr) == value))
        if type:
            stmt = stmt.where(Item.type == type)
        if created_from_dt is not None:
            stmt = stmt.where(Item.created_at >= created_from_dt)
        if created_to_dt is not None:
            stmt = stmt.where(Item.created_at <= created_to_dt)
        # Expire filters: by default a from/to range only matches items that
        # actually have an expire_at. ``never_expires=True`` flips that to
        # NULL-only; ``never_expires=False`` excludes NULLs but keeps range.
        if expired_only:
            stmt = stmt.where(Item.expire_at.isnot(None)).where(Item.expire_at < now)
        if expire_from_dt is not None:
            stmt = stmt.where(Item.expire_at.isnot(None)).where(Item.expire_at >= expire_from_dt)
        if expire_to_dt is not None:
            stmt = stmt.where(Item.expire_at.isnot(None)).where(Item.expire_at <= expire_to_dt)
        if never_expires is True:
            stmt = stmt.where(Item.expire_at.is_(None))
        elif never_expires is False:
            stmt = stmt.where(Item.expire_at.isnot(None))
        return stmt

    base = _apply(base)
    count_base = _apply(count_base)

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


def _parse_date_filter(field: str, raw: Optional[str]) -> Optional[datetime]:
    """Parse a date string accepting either ISO 8601 (with TZ) or ``YYYY-MM-DD``.

    ``YYYY-MM-DD`` is interpreted at 00:00:00 UTC. Naive ISO 8601 inputs are
    coerced to UTC. Returns ``None`` for empty input. Raises ``HTTPException``
    400 on invalid input so the client gets a clear error.
    """
    if not raw:
        return None
    s = raw.strip()
    # Bare date — normalise to UTC midnight.
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.strptime(s, "%Y-%m-%d")
            return d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass  # fall through to ISO 8601 attempt
    # ISO 8601. Python <3.11 doesn't accept trailing 'Z' — patch it.
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid {field}={raw!r}: expected ISO 8601 "
                "(e.g. 2025-01-15T00:00:00Z) or YYYY-MM-DD."
            ),
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
