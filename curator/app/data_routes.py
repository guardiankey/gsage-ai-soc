"""Curator — /data/ static file server with directory listing.

Serves the generated reputation list files from the data directory.
No authentication required — accessible only from within the Docker
internal network (gsage-internal).

Routes:
    GET /data/                                                 — HTML directory listing of available collections
    GET /data/{slug}/                                          — HTML directory listing of files in a collection
    GET /data/{slug}/{filename}                                — Download a specific file
    GET /data/{slug}/differentials/                            — List of years with diff data
    GET /data/{slug}/differentials/{year}/                     — List of months in a year
    GET /data/{slug}/differentials/{year}/{month}/             — List of days + monthly diff files
    GET /data/{slug}/differentials/{year}/{month}/{day}/       — List of daily diff files
    GET /data/{slug}/differentials/{year}/{month}/{filename}   — Download monthly diff file
    GET /data/{slug}/differentials/{year}/{month}/{day}/{filename}
                                                               — Download daily diff file
"""

from __future__ import annotations

import calendar
import datetime as _dt
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .diffs import (
    CollectionWindow,
    date_in_window,
    diff_file,
    ensure_daily_file,
    ensure_monthly_file,
    get_collection_window,
    iter_days_in_month_window,
    iter_months_in_window,
    iter_years_in_window,
    month_in_window,
)
from .models import ITEM_TYPES, Collection, Item

router = APIRouter(prefix="/data", tags=["data"])


def _data_root() -> Path:
    return Path(get_settings().data_dir)


def _html_page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head>"
        f"<meta charset='utf-8'><title>{title}</title>"
        "<style>"
        "body{font-family:monospace;padding:1em;}"
        "h1{border-bottom:1px solid #ccc;padding-bottom:.4em;}"
        "table{border-collapse:collapse;width:100%;}"
        "tr:hover{background:#f5f5f5;}"
        "td,th{text-align:left;padding:.3em .8em;}"
        "th{border-bottom:1px solid #ddd;color:#555;}"
        "a{text-decoration:none;color:#0366d6;}"
        "a:hover{text-decoration:underline;}"
        ".size{color:#888;text-align:right;}"
        ".meta{color:#888;}"
        "</style>"
        "</head><body>"
        f"<h1>{title}</h1>"
        f"{body}"
        "</body></html>"
    )


@router.get("/", response_class=HTMLResponse)
async def list_root(session: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """HTML directory listing of all active collections."""
    root = _data_root()
    root.mkdir(parents=True, exist_ok=True)

    count_sq = (
        select(Item.collection_id, func.count(Item.id).label("item_count"))
        .group_by(Item.collection_id)
        .subquery()
    )
    stmt = (
        select(Collection, func.coalesce(count_sq.c.item_count, 0).label("item_count"))
        .outerjoin(count_sq, Collection.id == count_sq.c.collection_id)
        .where(Collection.active.is_(True))
        .order_by(Collection.slug)
    )
    result = await session.execute(stmt)
    rows = result.all()

    rows_html = ""
    for col, item_count in rows:
        slug_dir = root / col.slug
        lastupdated = "-"
        ts_file = slug_dir / "lastupdated"
        if ts_file.exists():
            lastupdated = ts_file.read_text(encoding="utf-8").strip()
        subtype = col.subtype or "-"
        rows_html += (
            f"<tr>"
            f"<td><a href='/lists/{col.slug}/'>{col.slug}/</a></td>"
            f"<td class='meta'>{col.type}</td>"
            f"<td class='meta'>{subtype}</td>"
            f"<td class='meta'>{col.short_description}</td>"
            f"<td class='size'>{item_count:,} items</td>"
            f"<td class='meta'>{lastupdated}</td>"
            f"</tr>\n"
        )

    body = (
        "<table>"
        "<tr><th>Collection</th><th>Type</th><th>Subtype</th>"
        "<th>Description</th><th>Items</th><th>Last Updated</th></tr>\n"
        + rows_html
        + "</table>"
    )
    return HTMLResponse(_html_page("Index of /lists/", body))


@router.get("/{slug}/", response_class=HTMLResponse)
async def list_collection(slug: str, session: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """HTML directory listing of files inside a specific collection."""
    _guard_path(slug)

    # Fetch collection metadata and item count from DB
    count_sq = (
        select(Item.collection_id, func.count(Item.id).label("item_count"))
        .group_by(Item.collection_id)
        .subquery()
    )
    stmt = (
        select(Collection, func.coalesce(count_sq.c.item_count, 0).label("item_count"))
        .outerjoin(count_sq, Collection.id == count_sq.c.collection_id)
        .where(Collection.slug == slug)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Collection '{slug}' not found")

    col, item_count = row

    # Collection info card
    subtype_display = f" / {col.subtype}" if col.subtype else ""
    description_display = col.description or col.short_description
    lastupdated = "-"
    slug_dir = _data_root() / slug
    ts_file = slug_dir / "lastupdated"
    if ts_file.exists():
        lastupdated = ts_file.read_text(encoding="utf-8").strip()

    info_html = (
        "<table style='margin-bottom:1.5em;width:auto;'>"
        f"<tr><th>Description</th><td>{description_display}</td></tr>"
        f"<tr><th>Type</th><td>{col.type}{subtype_display}</td></tr>"
        f"<tr><th>Status</th><td>{col.status}</td></tr>"
        f"<tr><th>Items in DB</th><td>{item_count:,}</td></tr>"
        f"<tr><th>Last updated</th><td>{lastupdated}</td></tr>"
        "</table>"
    )

    # File listing from filesystem
    rows_html = "<tr><td><a href='/lists/'>../</a></td><td></td><td></td></tr>\n"
    rows_html += (
        f"<tr>"
        f"<td><a href='/lists/{slug}/differentials/'>differentials/</a></td>"
        f"<td class='size'></td>"
        f"<td class='meta'>per-day '+ added' / '- removed' diffs</td>"
        f"</tr>\n"
    )
    has_files = False
    if slug_dir.is_dir():
        for entry in sorted(slug_dir.iterdir()):
            if entry.is_file() and entry.name != "lastupdated":
                has_files = True
                stat = entry.stat()
                size = stat.st_size
                if size >= 1_048_576:
                    size_str = f"{size / 1_048_576:.1f} MB"
                elif size >= 1_024:
                    size_str = f"{size / 1_024:.1f} KB"
                else:
                    size_str = f"{size} B"
                modified = _dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                rows_html += (
                    f"<tr>"
                    f"<td><a href='/lists/{slug}/{entry.name}'>{entry.name}</a></td>"
                    f"<td class='size'>{size_str}</td>"
                    f"<td class='meta'>{modified}</td>"
                    f"</tr>\n"
                )

    if has_files:
        file_table = (
            "<table>"
            "<tr><th>File</th><th>Size</th><th>Modified</th></tr>\n"
            + rows_html
            + "</table>"
        )
    else:
        file_table = (
            "<table>"
            "<tr><th>File</th><th>Size</th><th>Modified</th></tr>\n"
            + rows_html
            + "</table>"
            "<p class='meta'>No files available yet.</p>"
        )

    body = info_html + file_table
    return HTMLResponse(_html_page(f"Index of /lists/{slug}/", body))


# ─── Differentials routes ────────────────────────────────────────────────────
# These must be registered BEFORE the generic `/{slug}/{filename}` catch-all so
# that paths like `/lists/<slug>/differentials/...` are not absorbed by it.


async def _load_collection_or_404(slug: str, session: AsyncSession) -> Collection:
    _guard_path(slug)
    result = await session.execute(select(Collection).where(Collection.slug == slug))
    col = result.scalar_one_or_none()
    if col is None:
        raise HTTPException(status_code=404, detail=f"Collection '{slug}' not found")
    return col


async def _window_or_404(session: AsyncSession, col: Collection) -> CollectionWindow:
    window = await get_collection_window(session, col.id)
    if window is None:
        raise HTTPException(status_code=404, detail="Collection has no items yet")
    return window


def _validate_ymd(year: int, month: int | None = None, day: int | None = None) -> None:
    if year < 1970 or year > 9999:
        raise HTTPException(status_code=400, detail="Invalid year")
    if month is not None and not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="Invalid month")
    if day is not None:
        last = calendar.monthrange(year, month or 1)[1]
        if not (1 <= day <= last):
            raise HTTPException(status_code=400, detail="Invalid day")


def _diff_filename_parts(col: Collection, filename: str) -> tuple[str, bool]:
    """Validate a diff filename and return (item_type, is_metadata).

    Format: ``<col.type>_<item_type>[_metadata].txt``
    """
    if not filename.endswith(".txt"):
        raise HTTPException(status_code=404, detail="File not found")
    base = filename[:-4]
    metadata = False
    if base.endswith("_metadata"):
        metadata = True
        base = base[: -len("_metadata")]
    expected_prefix = f"{col.type}_"
    if not base.startswith(expected_prefix):
        raise HTTPException(status_code=404, detail="File not found")
    item_type = base[len(expected_prefix):]
    if item_type not in ITEM_TYPES:
        raise HTTPException(status_code=404, detail="File not found")
    return item_type, metadata


@router.get("/{slug}/differentials/", response_class=HTMLResponse)
async def list_diff_years(slug: str, session: AsyncSession = Depends(get_db)) -> HTMLResponse:
    col = await _load_collection_or_404(slug, session)
    window = await get_collection_window(session, col.id)

    title = f"Index of /lists/{slug}/differentials/"

    if window is None:
        body = (
            "<p class='meta'>No differential data yet — the collection has no items.</p>"
            "<table><tr><th>Year</th><th></th></tr>\n"
            f"<tr><td><a href='/lists/{slug}/'>../</a></td><td class='meta'></td></tr>\n"
            "</table>"
        )
        return HTMLResponse(_html_page(title, body))

    rows = (
        f"<tr><td><a href='/lists/{slug}/'>../</a></td><td class='meta'></td></tr>\n"
    )
    for y in iter_years_in_window(window):
        rows += (
            f"<tr><td><a href='/lists/{slug}/differentials/{y:04d}/'>{y:04d}/</a></td>"
            f"<td class='meta'></td></tr>\n"
        )

    info = (
        "<p class='meta'>"
        f"Visible window: <b>{window.start.isoformat()}</b> &rarr; <b>{window.end.isoformat()}</b>"
        "</p>"
    )
    body = info + "<table><tr><th>Year</th><th></th></tr>\n" + rows + "</table>"
    return HTMLResponse(_html_page(title, body))


@router.get("/{slug}/differentials/{year}/", response_class=HTMLResponse)
async def list_diff_months(
    slug: str, year: int, session: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    col = await _load_collection_or_404(slug, session)
    window = await _window_or_404(session, col)
    _validate_ymd(year)

    months = [(y, m) for (y, m) in iter_months_in_window(window) if y == year]
    if not months:
        raise HTTPException(status_code=404, detail="Year out of window")

    rows = (
        f"<tr><td><a href='/lists/{slug}/differentials/'>../</a></td><td class='meta'></td></tr>\n"
    )
    for _, m in months:
        rows += (
            f"<tr><td><a href='/lists/{slug}/differentials/{year:04d}/{m:02d}/'>{m:02d}/</a></td>"
            f"<td class='meta'>{calendar.month_name[m]}</td></tr>\n"
        )

    body = "<table><tr><th>Month</th><th></th></tr>\n" + rows + "</table>"
    return HTMLResponse(
        _html_page(f"Index of /lists/{slug}/differentials/{year:04d}/", body)
    )


@router.get("/{slug}/differentials/{year}/{month}/", response_class=HTMLResponse)
async def list_diff_days(
    slug: str, year: int, month: int, session: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    col = await _load_collection_or_404(slug, session)
    window = await _window_or_404(session, col)
    _validate_ymd(year, month)

    if not month_in_window(window, year, month):
        raise HTTPException(status_code=404, detail="Month out of window")

    days = iter_days_in_month_window(window, year, month)

    rows = (
        f"<tr><td><a href='/lists/{slug}/differentials/{year:04d}/'>../</a></td>"
        f"<td class='meta'></td></tr>\n"
    )
    for d in days:
        rows += (
            f"<tr><td><a href='/lists/{slug}/differentials/{year:04d}/{month:02d}/{d.day:02d}/'>"
            f"{d.day:02d}/</a></td>"
            f"<td class='meta'>{d.isoformat()}</td></tr>\n"
        )

    # Inline links to monthly diff files (lazy generation on click)
    monthly_links = ""
    for itype in ITEM_TYPES:
        plain_name = f"{col.type}_{itype}.txt"
        meta_name = f"{col.type}_{itype}_metadata.txt"
        monthly_links += (
            f"<tr>"
            f"<td><a href='/lists/{slug}/differentials/{year:04d}/{month:02d}/{plain_name}'>{plain_name}</a></td>"
            f"<td><a href='/lists/{slug}/differentials/{year:04d}/{month:02d}/{meta_name}'>{meta_name}</a></td>"
            f"</tr>\n"
        )

    monthly_html = (
        "<h2 style='font-size:1em;color:#555;margin-top:1.5em;'>Monthly net change</h2>"
        "<table style='width:auto;'>"
        "<tr><th>Plain</th><th>Metadata</th></tr>\n"
        + monthly_links
        + "</table>"
    )

    body = (
        "<table><tr><th>Day</th><th></th></tr>\n" + rows + "</table>" + monthly_html
    )
    return HTMLResponse(
        _html_page(f"Index of /lists/{slug}/differentials/{year:04d}/{month:02d}/", body)
    )


@router.get(
    "/{slug}/differentials/{year}/{month}/{day_or_file}/", response_class=HTMLResponse
)
async def list_diff_day_files(
    slug: str,
    year: int,
    month: int,
    day_or_file: str,
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    col = await _load_collection_or_404(slug, session)
    window = await _window_or_404(session, col)

    # day_or_file must be a 2-digit day; otherwise this route doesn't apply.
    if not (day_or_file.isdigit() and len(day_or_file) <= 2):
        raise HTTPException(status_code=404, detail="Not found")
    day = int(day_or_file)
    _validate_ymd(year, month, day)

    cur_date = date(year, month, day)
    if not date_in_window(window, cur_date):
        raise HTTPException(status_code=404, detail="Day out of window")

    rows = (
        f"<tr><td><a href='/lists/{slug}/differentials/{year:04d}/{month:02d}/'>../</a></td>"
        f"<td></td></tr>\n"
    )
    for itype in ITEM_TYPES:
        plain_name = f"{col.type}_{itype}.txt"
        meta_name = f"{col.type}_{itype}_metadata.txt"
        rows += (
            f"<tr>"
            f"<td><a href='/lists/{slug}/differentials/{year:04d}/{month:02d}/{day:02d}/{plain_name}'>{plain_name}</a></td>"
            f"<td><a href='/lists/{slug}/differentials/{year:04d}/{month:02d}/{day:02d}/{meta_name}'>{meta_name}</a></td>"
            f"</tr>\n"
        )

    info = (
        f"<p class='meta'>Date: <b>{cur_date.isoformat()}</b></p>"
    )
    body = (
        info
        + "<table><tr><th>Plain</th><th>Metadata</th></tr>\n"
        + rows
        + "</table>"
    )
    return HTMLResponse(
        _html_page(
            f"Index of /lists/{slug}/differentials/{year:04d}/{month:02d}/{day:02d}/",
            body,
        )
    )


@router.get("/{slug}/differentials/{year}/{month}/{day}/{filename}")
async def get_daily_diff(
    slug: str,
    year: int,
    month: int,
    day: int,
    filename: str,
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    _guard_path(filename)
    col = await _load_collection_or_404(slug, session)
    window = await _window_or_404(session, col)
    _validate_ymd(year, month, day)

    cur_date = date(year, month, day)
    if not date_in_window(window, cur_date):
        raise HTTPException(status_code=404, detail="Day out of window")

    item_type, metadata = _diff_filename_parts(col, filename)
    path = await ensure_daily_file(col, item_type, year, month, day, metadata=metadata)
    return FileResponse(
        path=str(path), media_type="text/plain; charset=utf-8", filename=filename
    )


@router.get("/{slug}/differentials/{year}/{month}/{filename}")
async def get_monthly_diff(
    slug: str,
    year: int,
    month: int,
    filename: str,
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    _guard_path(filename)
    # Filename must contain a '.' (otherwise it could collide with a 2-digit day
    # which is handled by list_diff_day_files at a different route shape).
    if "." not in filename:
        raise HTTPException(status_code=404, detail="Not found")

    col = await _load_collection_or_404(slug, session)
    window = await _window_or_404(session, col)
    _validate_ymd(year, month)

    if not month_in_window(window, year, month):
        raise HTTPException(status_code=404, detail="Month out of window")

    item_type, metadata = _diff_filename_parts(col, filename)
    path = await ensure_monthly_file(col, item_type, year, month, metadata=metadata)
    return FileResponse(
        path=str(path), media_type="text/plain; charset=utf-8", filename=filename
    )


@router.get("/{slug}/{filename}")
async def get_file(slug: str, filename: str) -> FileResponse:
    """Download a specific file from a collection directory."""
    _guard_path(slug)
    _guard_path(filename)

    file_path = _data_root() / slug / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File '{filename}' not found in collection '{slug}'")

    media_type = "text/plain; charset=utf-8"
    return FileResponse(path=str(file_path), media_type=media_type, filename=filename)


def _guard_path(component: str) -> None:
    """Prevent path traversal: reject components with '/' or '..'."""
    if "/" in component or "\\" in component or ".." in component:
        raise HTTPException(status_code=400, detail="Invalid path component")



