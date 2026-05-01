"""gSage AI — RT managerial dashboard helpers.

Each helper issues bounded TicketSQL queries against the RT REST 2.0 API
and returns a structured dict ready to be emitted by the ``rt_dashboard``
tool. To keep latency and payload size predictable, every helper imposes
a hard cap on the number of tickets fetched and surfaces a ``truncated``
flag.

Time fields handled here:

* ``Created`` / ``Resolved`` / ``LastUpdated`` are returned by RT as ISO
  8601 strings in UTC. They are parsed with :func:`datetime.fromisoformat`
  (after stripping any trailing ``Z``).
* ``Due`` is RT's SLA / due-date field. Values <= ``Now`` mean the SLA is
  breached.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.mcp_server.tools.soc.ticket.rt._client import RTClient, RTError

log = logging.getLogger(__name__)

# Hard cap for any single dashboard view. Larger windows must be requested
# with narrower filters.
_MAX_TICKETS_PER_VIEW = 500
_DEFAULT_FIELDS = "id,Subject,Status,Queue,Owner,Created,LastUpdated,Resolved,Due"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw: Any) -> Optional[datetime]:
    """Parse an RT ISO 8601 timestamp; return ``None`` on missing/bogus input."""
    if not raw or raw in ("Not set", "1970-01-01T00:00:00Z"):
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _queue_clause(queues: Optional[list[str]]) -> str:
    """Build a TicketSQL fragment restricting ``Queue`` to *queues* (OR-joined)."""
    if not queues:
        return ""
    parts = [f"Queue = '{q}'" for q in queues if q]
    if not parts:
        return ""
    return "(" + " OR ".join(parts) + ")"


def _and_join(*clauses: str) -> str:
    return " AND ".join(c for c in clauses if c)


def _row_field(row: dict, *names: str) -> Any:
    """Return the first non-empty value among *names* from an RT row."""
    for n in names:
        v = row.get(n)
        if v not in (None, "", "Nobody", "Nobody in particular"):
            return v
    return None


def _normalise_owner(raw: Any) -> str:
    """RT may return Owner as a dict, an id, or a username — collapse to a name."""
    if raw in (None, "", "Nobody", "Nobody in particular"):
        return "Nobody"
    if isinstance(raw, dict):
        return raw.get("id") or raw.get("Name") or raw.get("name") or "Nobody"
    return str(raw)


# ── Views ───────────────────────────────────────────────────────────────


async def by_queue_status(
    client: RTClient,
    *,
    queues: Optional[list[str]] = None,
) -> dict:
    """Counts of tickets per (queue, status bucket).

    Status buckets are RT-canonical: ``new``, ``open``, ``stalled``,
    ``resolved`` (past 30 days only), ``rejected``, ``deleted`` (excluded).

    Returns
    -------
    dict
        ``{"queues": [{"queue": str, "new": int, "open": int, "stalled": int,
        "resolved_30d": int, "total_active": int}], "truncated": bool}``
    """
    queue_clause = _queue_clause(queues)
    cutoff_resolved = (_now_utc() - timedelta(days=30)).strftime("%Y-%m-%d")
    # We pull all "interesting" tickets (active + resolved-30d) and bucket in Python.
    # That's a single round-trip per dashboard call.
    query = _and_join(
        queue_clause,
        f"(Status='new' OR Status='open' OR Status='stalled' "
        f"OR (Status='resolved' AND Resolved > '{cutoff_resolved}'))",
    )
    rows = await _safe_search(client, query=query, fields=_DEFAULT_FIELDS)
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    grouped: dict[str, dict[str, int]] = defaultdict(
        lambda: {"new": 0, "open": 0, "stalled": 0, "resolved_30d": 0}
    )
    for row in rows:
        queue = str(_row_field(row, "Queue") or "?")
        status = str(_row_field(row, "Status") or "").lower()
        if status == "resolved":
            grouped[queue]["resolved_30d"] += 1
        elif status in {"new", "open", "stalled"}:
            grouped[queue][status] += 1

    out = []
    for queue, counts in sorted(grouped.items()):
        out.append({
            "queue": queue,
            **counts,
            "total_active": counts["new"] + counts["open"] + counts["stalled"],
        })
    return {"queues": out, "truncated": truncated}


async def by_owner(
    client: RTClient,
    *,
    queues: Optional[list[str]] = None,
    top_n: int = 20,
) -> dict:
    """Active workload by Owner across the chosen queues.

    Returns
    -------
    dict
        ``{"owners": [{"owner": str, "active": int, "stalled": int,
        "oldest_days": int|None}], "truncated": bool}``
    """
    queue_clause = _queue_clause(queues)
    query = _and_join(
        queue_clause,
        "(Status='new' OR Status='open' OR Status='stalled')",
    )
    rows = await _safe_search(client, query=query, fields=_DEFAULT_FIELDS)
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    now = _now_utc()
    workload: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"active": 0, "stalled": 0, "oldest_days": None}
    )
    for row in rows:
        owner = _normalise_owner(_row_field(row, "Owner"))
        status = str(_row_field(row, "Status") or "").lower()
        workload[owner]["active"] += 1
        if status == "stalled":
            workload[owner]["stalled"] += 1
        created = _parse_dt(_row_field(row, "Created"))
        if created:
            age = (now - created).days
            cur = workload[owner]["oldest_days"]
            if cur is None or age > cur:
                workload[owner]["oldest_days"] = age

    sorted_rows = sorted(
        workload.items(), key=lambda kv: kv[1]["active"], reverse=True
    )[:top_n]
    return {
        "owners": [{"owner": k, **v} for k, v in sorted_rows],
        "truncated": truncated,
    }


async def stalled(
    client: RTClient,
    *,
    queues: Optional[list[str]] = None,
    days_threshold: int = 7,
    top_n: int = 50,
) -> dict:
    """Tickets idle for at least ``days_threshold`` days (no LastUpdated).

    Returns
    -------
    dict
        ``{"tickets": [...], "threshold_days": int, "truncated": bool}``
    """
    cutoff = (_now_utc() - timedelta(days=days_threshold)).strftime("%Y-%m-%d %H:%M:%S")
    queue_clause = _queue_clause(queues)
    query = _and_join(
        queue_clause,
        "(Status='new' OR Status='open' OR Status='stalled')",
        f"LastUpdated < '{cutoff}'",
    )
    rows = await _safe_search(client, query=query, fields=_DEFAULT_FIELDS)
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    now = _now_utc()
    enriched: list[dict] = []
    for row in rows:
        last = _parse_dt(_row_field(row, "LastUpdated"))
        idle_days = (now - last).days if last else None
        enriched.append({
            "id": _row_field(row, "id"),
            "subject": _row_field(row, "Subject"),
            "queue": _row_field(row, "Queue"),
            "status": _row_field(row, "Status"),
            "owner": _normalise_owner(_row_field(row, "Owner")),
            "last_updated": _row_field(row, "LastUpdated"),
            "idle_days": idle_days,
        })
    enriched.sort(key=lambda r: r["idle_days"] or 0, reverse=True)
    return {
        "tickets": enriched[:top_n],
        "threshold_days": days_threshold,
        "truncated": truncated,
    }


async def sla_breaches(
    client: RTClient,
    *,
    queues: Optional[list[str]] = None,
    window_days: int = 7,
) -> dict:
    """Tickets with ``Due`` either past or within the next *window_days* days.

    Returns
    -------
    dict
        ``{"breached": [...], "near_breach": [...], "window_days": int,
        "truncated": bool}``
    """
    now = _now_utc()
    soon = now + timedelta(days=window_days)
    queue_clause = _queue_clause(queues)
    # RT supports Due > and Due < but not BETWEEN; we filter both buckets in
    # one query and split in Python.
    query = _and_join(
        queue_clause,
        "(Status='new' OR Status='open' OR Status='stalled')",
        f"Due > '1971-01-01' AND Due < '{soon.strftime('%Y-%m-%d %H:%M:%S')}'",
    )
    rows = await _safe_search(client, query=query, fields=_DEFAULT_FIELDS)
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    breached: list[dict] = []
    near: list[dict] = []
    for row in rows:
        due = _parse_dt(_row_field(row, "Due"))
        if not due:
            continue
        item = {
            "id": _row_field(row, "id"),
            "subject": _row_field(row, "Subject"),
            "queue": _row_field(row, "Queue"),
            "status": _row_field(row, "Status"),
            "owner": _normalise_owner(_row_field(row, "Owner")),
            "due": _row_field(row, "Due"),
            "hours_remaining": int((due - now).total_seconds() / 3600),
        }
        (breached if due <= now else near).append(item)

    breached.sort(key=lambda r: r["hours_remaining"])
    near.sort(key=lambda r: r["hours_remaining"])
    return {
        "breached": breached,
        "near_breach": near,
        "window_days": window_days,
        "truncated": truncated,
    }


async def top_requesters(
    client: RTClient,
    *,
    queues: Optional[list[str]] = None,
    days: int = 30,
    top_n: int = 20,
) -> dict:
    """Top requesters by ticket count over the last ``days`` days.

    Note: ``Requestors`` is a list field. RT's TicketSQL exposes it via
    ``Requestor.EmailAddress``. We rely on the search-result row for the
    canonical ``Requestor`` (first one).
    """
    cutoff = (_now_utc() - timedelta(days=days)).strftime("%Y-%m-%d")
    queue_clause = _queue_clause(queues)
    query = _and_join(queue_clause, f"Created > '{cutoff}'")
    rows = await _safe_search(
        client,
        query=query,
        fields=_DEFAULT_FIELDS + ",Requestor",
    )
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    counter: Counter[str] = Counter()
    for row in rows:
        req = _row_field(row, "Requestor", "Requestors")
        if isinstance(req, list) and req:
            req = req[0]
        if isinstance(req, dict):
            req = req.get("id") or req.get("EmailAddress") or req.get("Name")
        counter[str(req or "unknown")] += 1

    return {
        "requesters": [
            {"requester": k, "count": v}
            for k, v in counter.most_common(top_n)
        ],
        "days": days,
        "total_tickets": sum(counter.values()),
        "truncated": truncated,
    }


async def mean_ttr(
    client: RTClient,
    *,
    queues: Optional[list[str]] = None,
    days: int = 30,
) -> dict:
    """Mean time-to-resolve per queue over the last ``days`` days.

    Returns
    -------
    dict
        ``{"queues": [{"queue": str, "tickets_resolved": int,
        "mean_hours": float, "p95_hours": float}], "truncated": bool}``
    """
    cutoff = (_now_utc() - timedelta(days=days)).strftime("%Y-%m-%d")
    queue_clause = _queue_clause(queues)
    query = _and_join(
        queue_clause,
        "Status='resolved'",
        f"Resolved > '{cutoff}'",
    )
    rows = await _safe_search(client, query=query, fields=_DEFAULT_FIELDS)
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    per_queue: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        created = _parse_dt(_row_field(row, "Created"))
        resolved = _parse_dt(_row_field(row, "Resolved"))
        if not (created and resolved and resolved > created):
            continue
        hours = (resolved - created).total_seconds() / 3600.0
        queue = str(_row_field(row, "Queue") or "?")
        per_queue[queue].append(hours)

    out = []
    for queue, samples in sorted(per_queue.items()):
        samples_sorted = sorted(samples)
        n = len(samples_sorted)
        mean = sum(samples_sorted) / n if n else 0.0
        p95 = samples_sorted[max(0, int(n * 0.95) - 1)] if n else 0.0
        out.append({
            "queue": queue,
            "tickets_resolved": n,
            "mean_hours": round(mean, 2),
            "p95_hours": round(p95, 2),
        })
    return {"queues": out, "days": days, "truncated": truncated}


async def trend(
    client: RTClient,
    *,
    queues: Optional[list[str]] = None,
    days: int = 14,
    granularity: str = "day",
) -> dict:
    """Daily/weekly trend of created vs. resolved tickets.

    Granularity: ``"day"`` (default) or ``"week"`` (ISO week starts Monday).
    """
    if granularity not in {"day", "week"}:
        granularity = "day"
    cutoff = _now_utc() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    queue_clause = _queue_clause(queues)
    # Single OR-query; we'll bucket each ticket twice (created + resolved) if both
    # fall inside the window.
    query = _and_join(
        queue_clause,
        f"(Created > '{cutoff_str}' OR Resolved > '{cutoff_str}')",
    )
    rows = await _safe_search(client, query=query, fields=_DEFAULT_FIELDS)
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"created": 0, "resolved": 0})

    def _bucket_key(dt: datetime) -> str:
        if granularity == "week":
            year, week, _ = dt.isocalendar()
            return f"{year}-W{week:02d}"
        return dt.strftime("%Y-%m-%d")

    for row in rows:
        created = _parse_dt(_row_field(row, "Created"))
        if created and created >= cutoff:
            buckets[_bucket_key(created)]["created"] += 1
        resolved = _parse_dt(_row_field(row, "Resolved"))
        if resolved and resolved >= cutoff:
            buckets[_bucket_key(resolved)]["resolved"] += 1

    series = [
        {"bucket": k, "created": v["created"], "resolved": v["resolved"]}
        for k, v in sorted(buckets.items())
    ]
    return {
        "series": series,
        "granularity": granularity,
        "days": days,
        "truncated": truncated,
    }


# ── Internal: bounded search ────────────────────────────────────────────


async def _safe_search(
    client: RTClient,
    *,
    query: str,
    fields: str,
) -> list[dict]:
    """Issue a search capped at ``_MAX_TICKETS_PER_VIEW`` rows.

    Wraps :class:`RTError` to make sure callers can surface the upstream
    failure with a stable code.
    """
    if not query:
        # An empty TicketSQL would match everything — refuse to flood RT.
        raise RTError(
            "Dashboard query is empty (no filter). Provide queues to scope the view.",
            code="INVALID_PARAMS",
        )
    rows: list[dict] = []
    async for row in client.search_tickets_iter(query=query, fields=fields):
        rows.append(row)
        if len(rows) >= _MAX_TICKETS_PER_VIEW:
            break
    return rows
