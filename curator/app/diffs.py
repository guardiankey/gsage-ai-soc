"""Curator — daily and monthly differential file computation.

Differential files describe the changes that happened to a collection on a
given UTC day (``+ value`` for additions, ``- value`` for removals) or as the
net change over a month.

Storage layout (under ``data_dir``):

    <slug>/differentials/YYYY/MM/DD/<type>_<itype>.txt
    <slug>/differentials/YYYY/MM/DD/<type>_<itype>_metadata.txt
    <slug>/differentials/YYYY/MM/<type>_<itype>.txt          (monthly)
    <slug>/differentials/YYYY/MM/<type>_<itype>_metadata.txt (monthly)

Files are generated lazily on first HTTP request and cached on disk. Past
closed periods are immutable; today and the current month are always
recomputed (atomic rewrite via tmp + ``os.replace``).

Window
------
Listings are bounded to ``[max(MIN(created_at).date, today - DIFF_RETENTION_DAYS), today]``
because rows soft-deleted longer than that are physically purged by the
``_purge_loop`` background task and the underlying data is gone.

Events
------
* ``+`` events come from ``created_at`` and ``re_added_at`` (a row that was
  soft-deleted and re-added gets a new ``re_added_at`` and ``deleted_at`` is
  cleared).
* ``-`` events come from ``deleted_at``.

Order inside a daily file follows the event timestamp (ascending).

Monthly net change
------------------
* ``+`` rows: the most recent add event (``COALESCE(re_added_at, created_at)``)
  falls inside the month *and* the row is still active at the end of the
  month.
* ``-`` rows: the most recent add event happened *before* the month started
  *and* ``deleted_at`` falls inside the month.

Items that were both added and removed inside the same month do not appear.
"""

from __future__ import annotations

import calendar
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import DIFF_RETENTION_DAYS, get_settings
from .database import get_engine
from .models import ITEM_TYPES, Collection, Item

log = logging.getLogger(__name__)


PeriodKind = str  # "day" | "month"

# itype is constrained at the route layer to ITEM_TYPES; type comes from the
# Collection.type column. Both are interpolated into SQL — make sure callers
# only pass values that originate from trusted sources (DB column / hardcoded
# constants).


# ── Window helpers ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CollectionWindow:
    """Visible date window for a collection's differentials."""

    start: date  # inclusive
    end: date  # inclusive (= today UTC)


async def get_collection_window(session: AsyncSession, collection_id: int) -> CollectionWindow | None:
    """Return the visible window or None if the collection has no rows yet."""
    today = datetime.now(tz=timezone.utc).date()
    floor = today - timedelta(days=DIFF_RETENTION_DAYS - 1)

    # Use the earliest "+" event timestamp across both created_at and re_added_at,
    # ignoring NULLs in re_added_at.
    stmt = select(func.min(Item.created_at)).where(Item.collection_id == collection_id)
    min_created = (await session.execute(stmt)).scalar_one_or_none()
    if min_created is None:
        return None

    min_date = min_created.astimezone(timezone.utc).date()
    start = max(min_date, floor)
    return CollectionWindow(start=start, end=today)


def date_in_window(window: CollectionWindow, d: date) -> bool:
    return window.start <= d <= window.end


def month_in_window(window: CollectionWindow, year: int, month: int) -> bool:
    """A month is in window if any of its days overlaps the visible window."""
    last_day = calendar.monthrange(year, month)[1]
    month_first = date(year, month, 1)
    month_last = date(year, month, last_day)
    return not (month_last < window.start or month_first > window.end)


def iter_days_in_month_window(window: CollectionWindow, year: int, month: int) -> list[date]:
    """Days of <year, month> intersected with the visible window."""
    last_day = calendar.monthrange(year, month)[1]
    out: list[date] = []
    for d in range(1, last_day + 1):
        cur = date(year, month, d)
        if date_in_window(window, cur):
            out.append(cur)
    return out


def iter_months_in_window(window: CollectionWindow) -> list[tuple[int, int]]:
    """Distinct (year, month) tuples inside the visible window."""
    out: list[tuple[int, int]] = []
    cur = date(window.start.year, window.start.month, 1)
    end = window.end
    while cur <= end:
        out.append((cur.year, cur.month))
        # advance one month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out


def iter_years_in_window(window: CollectionWindow) -> list[int]:
    return sorted({y for (y, _m) in iter_months_in_window(window)})


# ── Path helpers ──────────────────────────────────────────────────────────────


def _data_root() -> Path:
    return Path(get_settings().data_dir)


def diff_dir(slug: str, year: int, month: int, day: int | None = None) -> Path:
    base = _data_root() / slug / "differentials" / f"{year:04d}" / f"{month:02d}"
    if day is not None:
        base = base / f"{day:02d}"
    return base


def diff_file(slug: str, year: int, month: int, day: int | None,
              col_type: str, item_type: str, *, metadata: bool) -> Path:
    fname = f"{col_type}_{item_type}{'_metadata' if metadata else ''}.txt"
    return diff_dir(slug, year, month, day) / fname


# ── SQL templates ─────────────────────────────────────────────────────────────


_DAILY_PLAIN_QUERY = """
SELECT line FROM (
    SELECT created_at AS ts, '+ ' || COALESCE(host(cidr), value) AS line
    FROM curator_items
    WHERE collection_id = {cid}
      AND type = '{itype}'
      AND created_at >= '{day_start}'
      AND created_at <  '{day_end}'

    UNION ALL

    SELECT re_added_at AS ts, '+ ' || COALESCE(host(cidr), value) AS line
    FROM curator_items
    WHERE collection_id = {cid}
      AND type = '{itype}'
      AND re_added_at >= '{day_start}'
      AND re_added_at <  '{day_end}'

    UNION ALL

    SELECT deleted_at AS ts, '- ' || COALESCE(host(cidr), value) AS line
    FROM curator_items
    WHERE collection_id = {cid}
      AND type = '{itype}'
      AND deleted_at >= '{day_start}'
      AND deleted_at <  '{day_end}'
) t
ORDER BY ts
""".strip()


_META_TAIL = (
    " || ' # ' || TO_CHAR(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
    " || ' ' || COALESCE(TO_CHAR(expire_at AT TIME ZONE 'UTC', 'YYYY-MM-DD'), 'never')"
    " || ' ' || COALESCE(public_reference, '')"
)


_DAILY_META_QUERY = f"""
SELECT line FROM (
    SELECT created_at AS ts,
           '+ ' || COALESCE(host(cidr), value){_META_TAIL} AS line
    FROM curator_items
    WHERE collection_id = {{cid}}
      AND type = '{{itype}}'
      AND created_at >= '{{day_start}}'
      AND created_at <  '{{day_end}}'

    UNION ALL

    SELECT re_added_at AS ts,
           '+ ' || COALESCE(host(cidr), value){_META_TAIL} AS line
    FROM curator_items
    WHERE collection_id = {{cid}}
      AND type = '{{itype}}'
      AND re_added_at >= '{{day_start}}'
      AND re_added_at <  '{{day_end}}'

    UNION ALL

    SELECT deleted_at AS ts,
           '- ' || COALESCE(host(cidr), value){_META_TAIL} AS line
    FROM curator_items
    WHERE collection_id = {{cid}}
      AND type = '{{itype}}'
      AND deleted_at >= '{{day_start}}'
      AND deleted_at <  '{{day_end}}'
) t
ORDER BY ts
""".strip()


_MONTHLY_PLAIN_QUERY = """
SELECT line FROM (
    SELECT COALESCE(re_added_at, created_at) AS ts,
           '+ ' || COALESCE(host(cidr), value) AS line
    FROM curator_items
    WHERE collection_id = {cid}
      AND type = '{itype}'
      AND COALESCE(re_added_at, created_at) >= '{m_start}'
      AND COALESCE(re_added_at, created_at) <  '{m_end}'
      AND (deleted_at IS NULL OR deleted_at >= '{m_end}')

    UNION ALL

    SELECT deleted_at AS ts,
           '- ' || COALESCE(host(cidr), value) AS line
    FROM curator_items
    WHERE collection_id = {cid}
      AND type = '{itype}'
      AND COALESCE(re_added_at, created_at) < '{m_start}'
      AND deleted_at >= '{m_start}'
      AND deleted_at <  '{m_end}'
) t
ORDER BY ts
""".strip()


_MONTHLY_META_QUERY = f"""
SELECT line FROM (
    SELECT COALESCE(re_added_at, created_at) AS ts,
           '+ ' || COALESCE(host(cidr), value){_META_TAIL} AS line
    FROM curator_items
    WHERE collection_id = {{cid}}
      AND type = '{{itype}}'
      AND COALESCE(re_added_at, created_at) >= '{{m_start}}'
      AND COALESCE(re_added_at, created_at) <  '{{m_end}}'
      AND (deleted_at IS NULL OR deleted_at >= '{{m_end}}')

    UNION ALL

    SELECT deleted_at AS ts,
           '- ' || COALESCE(host(cidr), value){_META_TAIL} AS line
    FROM curator_items
    WHERE collection_id = {{cid}}
      AND type = '{{itype}}'
      AND COALESCE(re_added_at, created_at) < '{{m_start}}'
      AND deleted_at >= '{{m_start}}'
      AND deleted_at <  '{{m_end}}'
) t
ORDER BY ts
""".strip()


# ── Compute / write ───────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    # Postgres-compatible literal in UTC, ISO 8601 without timezone suffix.
    # All curator timestamps are UTC; we send naive timestamps and let Postgres
    # store them as timestamptz.
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")


async def _copy_to_file(asyncpg_conn, query: str, dest: Path) -> int:
    """Stream COPY output to *dest* atomically. Returns row count.

    Always replaces *dest* (creates an empty file if the query produced no rows).
    """
    tmp = dest.with_suffix(".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = await asyncpg_conn.copy_from_query(query, output=str(tmp))
        rows = int(status.split()[-1]) if status else 0
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return rows


def _is_closed_past(year: int, month: int, day: int | None, today: date) -> bool:
    """A period is "closed" once it is fully in the past relative to today UTC."""
    if day is not None:
        return date(year, month, day) < today
    # Monthly: closed if month_last < today
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day) < today


async def ensure_daily_file(
    collection: Collection,
    item_type: str,
    year: int,
    month: int,
    day: int,
    *,
    metadata: bool,
) -> Path:
    """Return path to the cached daily diff file, generating it if needed."""
    if item_type not in ITEM_TYPES:
        raise ValueError(f"invalid item_type: {item_type}")

    path = diff_file(collection.slug, year, month, day, collection.type, item_type, metadata=metadata)
    today = datetime.now(tz=timezone.utc).date()

    if path.exists() and _is_closed_past(year, month, day, today):
        return path

    day_start = datetime(year, month, day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    template = _DAILY_META_QUERY if metadata else _DAILY_PLAIN_QUERY
    query = template.format(
        cid=collection.id,
        itype=item_type,
        day_start=_iso(day_start),
        day_end=_iso(day_end),
    )

    engine = get_engine()
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        asyncpg_conn = raw.driver_connection
        await _copy_to_file(asyncpg_conn, query, path)
    return path


async def ensure_monthly_file(
    collection: Collection,
    item_type: str,
    year: int,
    month: int,
    *,
    metadata: bool,
) -> Path:
    """Return path to the cached monthly diff file, generating it if needed."""
    if item_type not in ITEM_TYPES:
        raise ValueError(f"invalid item_type: {item_type}")

    path = diff_file(collection.slug, year, month, None, collection.type, item_type, metadata=metadata)
    today = datetime.now(tz=timezone.utc).date()

    if path.exists() and _is_closed_past(year, month, None, today):
        return path

    m_start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        m_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        m_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    template = _MONTHLY_META_QUERY if metadata else _MONTHLY_PLAIN_QUERY
    query = template.format(
        cid=collection.id,
        itype=item_type,
        m_start=_iso(m_start),
        m_end=_iso(m_end),
    )

    engine = get_engine()
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        asyncpg_conn = raw.driver_connection
        await _copy_to_file(asyncpg_conn, query, path)
    return path
