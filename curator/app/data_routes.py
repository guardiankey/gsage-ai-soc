"""Curator — /data/ static file server with directory listing.

Serves the generated reputation list files from the data directory.
No authentication required — accessible only from within the Docker
internal network (gsage-internal).

Routes:
    GET /data/                      — HTML directory listing of available collections
    GET /data/{slug}/               — HTML directory listing of files in a collection
    GET /data/{slug}/{filename}     — Download a specific file
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .models import Collection, Item

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



