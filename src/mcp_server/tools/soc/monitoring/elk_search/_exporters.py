"""gSage AI — Result exporters for the elk_search tool (JSON / CSV / XLSX)."""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable


def _flatten(
    obj: Any,
    prefix: str = "",
    out: dict | None = None,
) -> dict[str, Any]:
    """Flatten a nested dict into dotted keys.

    Lists become JSON strings (keeps CSV/XLSX cells stable and searchable).
    """
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                _flatten(v, key, out)
            elif isinstance(v, list):
                out[key] = json.dumps(v, ensure_ascii=False, default=str)
            else:
                out[key] = v
    else:
        out[prefix] = obj
    return out


def _hit_row(hit: dict) -> dict[str, Any]:
    """Convert a raw ES hit into a flat row (keeps metadata fields prefixed)."""
    row: dict[str, Any] = {
        "_index": hit.get("_index"),
        "_id": hit.get("_id"),
        "_score": hit.get("_score"),
    }
    source = hit.get("_source") or {}
    row.update(_flatten(source))
    return row


def _collect_columns(rows: list[dict[str, Any]], fields: list[str] | None) -> list[str]:
    if fields:
        explicit = ["_index", "_id", *fields]
        seen: set[str] = set()
        ordered: list[str] = []
        for f in explicit:
            if f not in seen:
                seen.add(f)
                ordered.append(f)
        return ordered

    all_cols: list[str] = []
    seen_all: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen_all:
                seen_all.add(k)
                all_cols.append(k)
    return all_cols


def to_json(hits: Iterable[dict]) -> bytes:
    """Serialize hits as a pretty-printed JSON array (UTF-8 bytes)."""
    payload = [
        {
            "_index": h.get("_index"),
            "_id": h.get("_id"),
            "_score": h.get("_score"),
            "_source": h.get("_source") or {},
        }
        for h in hits
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def to_csv(hits: Iterable[dict], fields: list[str] | None = None) -> bytes:
    """Serialize hits to CSV bytes.  Nested objects are flattened with dotted keys."""
    rows = [_hit_row(h) for h in hits]
    columns = _collect_columns(rows, fields)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: _stringify(r.get(c)) for c in columns})
    return buf.getvalue().encode("utf-8")


def to_xlsx(hits: Iterable[dict], fields: list[str] | None = None) -> bytes:
    """Serialize hits to an XLSX workbook (bytes)."""
    from openpyxl import Workbook  # local import: optional heavy dep

    rows = [_hit_row(h) for h in hits]
    columns = _collect_columns(rows, fields)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="elk_search")
    ws.append(columns)
    for r in rows:
        ws.append([_stringify(r.get(c)) for c in columns])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _stringify(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(v)
