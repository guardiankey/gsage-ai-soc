"""gSage AI — Elasticsearch DSL builder for the elk_search tool.

Translates the tool's user-friendly parameters into a safe subset of
Elasticsearch Query DSL:

* ``query_string`` — free-form Lucene-style query (KQL-like).
* ``filters`` — list of simple ``{"field": "...", "value": "..."}`` or
  ``{"field": "...", "values": [...]}`` clauses (ANDed as ``term`` /
  ``terms``).
* ``time_range`` — either a preset keyword or explicit ISO ``{"gte":,
  "lte":}``.  Applied to the ``@timestamp`` field (Logstash/Beats
  convention).
* ``fields`` — list of fields to return via ``_source``.
* ``sort`` — list of ``"field:asc"`` / ``"field:desc"`` strings.
* ``size`` — number of hits to return (hard-capped by the caller).

Aggregations, scripts, runtime fields and scroll are intentionally **not
supported** in V1.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

TIME_FIELD = "@timestamp"

_TIME_PRESETS: dict[str, timedelta] = {
    "last_5m": timedelta(minutes=5),
    "last_15m": timedelta(minutes=15),
    "last_30m": timedelta(minutes=30),
    "last_hour": timedelta(hours=1),
    "last_4h": timedelta(hours=4),
    "last_12h": timedelta(hours=12),
    "last_day": timedelta(days=1),
    "last_week": timedelta(days=7),
}


class QueryBuildError(ValueError):
    """Raised when the caller-provided params produce an invalid DSL."""


def build_query(
    *,
    query_string: str | None = None,
    filters: list[dict] | None = None,
    time_range: dict | str | None = None,
    fields: list[str] | None = None,
    sort: list[str] | None = None,
    size: int = 50,
    time_field: str = TIME_FIELD,
) -> dict[str, Any]:
    """Build the ``_search`` request body."""
    must: list[dict] = []
    filter_clauses: list[dict] = []

    if query_string:
        must.append({
            "query_string": {
                "query": query_string,
                "analyze_wildcard": True,
                "default_operator": "AND",
            }
        })

    for f in filters or []:
        filter_clauses.append(_build_filter_clause(f))

    tr = _build_time_range(time_range, time_field)
    if tr is not None:
        filter_clauses.append(tr)

    query: dict[str, Any] = {"match_all": {}}
    if must or filter_clauses:
        query = {
            "bool": {
                "must": must or [{"match_all": {}}],
                "filter": filter_clauses,
            }
        }

    body: dict[str, Any] = {
        "size": max(0, int(size)),
        "query": query,
    }

    if fields:
        body["_source"] = list(fields)

    if sort:
        body["sort"] = [_build_sort_clause(s) for s in sort]
    else:
        # Sensible default: newest first if the time field exists.
        body["sort"] = [{time_field: {"order": "desc", "unmapped_type": "date"}}]

    return body


# ── Helpers ─────────────────────────────────────────────────────────────


def _build_filter_clause(f: dict) -> dict:
    if not isinstance(f, dict):
        raise QueryBuildError(f"Filter must be an object, got {type(f).__name__}.")
    field = f.get("field")
    if not field or not isinstance(field, str):
        raise QueryBuildError("Each filter needs a non-empty string 'field'.")

    if "values" in f:
        values = f["values"]
        if not isinstance(values, list) or not values:
            raise QueryBuildError(f"Filter for '{field}' has empty 'values'.")
        return {"terms": {field: list(values)}}

    if "value" in f:
        return {"term": {field: f["value"]}}

    if "exists" in f and f["exists"]:
        return {"exists": {"field": field}}

    raise QueryBuildError(
        f"Filter for '{field}' must provide 'value', 'values' or 'exists:true'."
    )


def _build_time_range(
    time_range: dict | str | None,
    time_field: str,
) -> dict | None:
    if not time_range:
        return None

    if isinstance(time_range, str):
        preset = _TIME_PRESETS.get(time_range)
        if preset is None:
            raise QueryBuildError(
                f"Unknown time_range preset '{time_range}'. Known: "
                f"{sorted(_TIME_PRESETS)}."
            )
        now = datetime.now(timezone.utc)
        return {
            "range": {
                time_field: {
                    "gte": (now - preset).isoformat(),
                    "lte": now.isoformat(),
                }
            }
        }

    if isinstance(time_range, dict):
        gte = time_range.get("gte")
        lte = time_range.get("lte")
        if not gte and not lte:
            raise QueryBuildError("Explicit time_range needs 'gte' and/or 'lte'.")
        clause: dict[str, Any] = {}
        if gte:
            clause["gte"] = gte
        if lte:
            clause["lte"] = lte
        return {"range": {time_field: clause}}

    raise QueryBuildError(
        f"Invalid time_range type {type(time_range).__name__}; "
        "expected string preset or object."
    )


def _build_sort_clause(spec: str) -> dict:
    if ":" in spec:
        field, _, order = spec.partition(":")
    else:
        field, order = spec, "asc"
    order = order.strip().lower() or "asc"
    if order not in ("asc", "desc"):
        raise QueryBuildError(f"Sort order must be 'asc' or 'desc' (got '{order}').")
    return {field.strip(): {"order": order, "unmapped_type": "keyword"}}
