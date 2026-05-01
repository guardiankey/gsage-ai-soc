"""gSage AI — GLPI managerial dashboard helpers.

Each helper issues bounded GLPI search queries and returns a structured
dict ready to be emitted by the ``glpi_dashboard`` tool. To keep latency
and payload size predictable, every helper imposes a hard cap on the
number of tickets fetched and surfaces a ``truncated`` flag.

GLPI search peculiarities handled here:

* Search rows are dicts keyed by the searchOption *field id* (as a
  string). With ``uid_cols=false`` (the default in :class:`GLPIClient`),
  values are the human-readable, locale-translated labels (e.g.
  ``"Novo"`` for status id 1). For counting we therefore prefer many
  cheap ``range=0-0`` queries that only consume ``totalcount`` rather
  than parsing labels.
* GLPI's criteria array is *flat*: nested OR groups are not supported by
  this client. Open-status filtering uses ``notequals`` against
  ``solved`` (5) and ``closed`` (6) so it composes with date filters via
  plain AND. Multi-status counting uses one query per status.
* Date fields are returned as ``YYYY-MM-DD HH:MM:SS`` strings.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError

log = logging.getLogger(__name__)

# Hard cap for any single dashboard view. Larger windows must be requested
# with narrower filters.
_MAX_TICKETS_PER_VIEW = 500
_GLPI_RANGE_FULL = f"0-{_MAX_TICKETS_PER_VIEW - 1}"
_GLPI_RANGE_COUNT_ONLY = "0-0"

# Standard GLPI Ticket searchOption IDs (default install).
_F_NAME = 1
_F_PRIORITY = 3
_F_REQUESTER = 4         # _Ticket_User type=1
_F_TECHNICIAN = 5        # _Ticket_User type=2
_F_TECH_GROUP = 8        # _Groups_Tickets type=2
_F_STATUS = 12
_F_DATE_CREATION = 15
_F_SOLVEDATE = 17
_F_DATE_MOD = 19
_F_REQUESTER_GROUP = 71

# Status code -> bucket name. GLPI ticket status default codes:
#   1=new, 2=assigned, 3=planned, 4=waiting, 5=solved, 6=closed
_STATUS_NEW = 1
_STATUS_ASSIGNED = 2
_STATUS_PLANNED = 3
_STATUS_WAITING = 4
_STATUS_SOLVED = 5
_STATUS_CLOSED = 6
_OPEN_STATUSES = (_STATUS_NEW, _STATUS_ASSIGNED, _STATUS_PLANNED, _STATUS_WAITING)
_STATUS_BUCKET_NAMES = {
    _STATUS_NEW: "new",
    _STATUS_ASSIGNED: "assigned",
    _STATUS_PLANNED: "planned",
    _STATUS_WAITING: "waiting",
    _STATUS_SOLVED: "solved",
    _STATUS_CLOSED: "closed",
}


# ── Time helpers ────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _glpi_dt(dt: datetime) -> str:
    """Format a datetime as GLPI expects in search criteria."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(raw: Any) -> Optional[datetime]:
    """Parse a GLPI date string to UTC datetime; ``None`` on missing/bogus."""
    if not raw or raw in ("", "0000-00-00 00:00:00", "0000-00-00"):
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    # GLPI returns naive timestamps in the server timezone — we treat them
    # as UTC for comparison purposes (good-enough for managerial views).
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ── Criteria builders ───────────────────────────────────────────────────


def _crit(field: int, searchtype: str, value: Any, *, link: str = "AND") -> dict:
    return {"link": link, "field": field, "searchtype": searchtype, "value": value}


def _open_status_criteria(start_link: str = "AND") -> list[dict]:
    """Restrict to non-solved/closed tickets via two ``notequals`` criteria."""
    return [
        _crit(_F_STATUS, "notequals", _STATUS_SOLVED, link=start_link),
        _crit(_F_STATUS, "notequals", _STATUS_CLOSED, link="AND"),
    ]


def _group_criterion(group_id: int, *, link: str = "AND") -> dict:
    return _crit(_F_TECH_GROUP, "equals", group_id, link=link)


def _row_field(row: dict, field_id: int) -> Any:
    """Return the value at ``field_id`` (or its string form) from a search row."""
    if field_id in row:
        return row[field_id]
    return row.get(str(field_id))


def _label(raw: Any) -> str:
    if raw in (None, "", 0, "0"):
        return "unknown"
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        return str(raw.get("name") or raw.get("id") or "unknown")
    return str(raw)


# ── Internal: bounded search ────────────────────────────────────────────


async def _safe_search(
    client: GLPIClient,
    *,
    criteria: list[dict],
    forcedisplay: list[int],
    range_str: str = _GLPI_RANGE_FULL,
    sort: Optional[int] = None,
    order: str = "ASC",
) -> dict:
    """Issue a search and normalise the response.

    Returns the raw GLPI dict (with keys ``totalcount``, ``count``,
    ``data``). The data list is already capped at ``_MAX_TICKETS_PER_VIEW``
    by the ``range_str`` argument.
    """
    return await client.search_items(
        "Ticket",
        criteria=criteria,
        forcedisplay=forcedisplay,
        range=range_str,
        sort=sort,
        order=order,
    )


async def _count(
    client: GLPIClient,
    *,
    criteria: list[dict],
) -> int:
    """Return ``totalcount`` for the given criteria using a 0-0 range."""
    res = await _safe_search(
        client,
        criteria=criteria,
        forcedisplay=[2],  # id only — keeps payload tiny
        range_str=_GLPI_RANGE_COUNT_ONLY,
    )
    try:
        return int(res.get("totalcount") or 0)
    except (TypeError, ValueError):
        return 0


# ── Views ───────────────────────────────────────────────────────────────


async def by_group_status(
    client: GLPIClient,
    *,
    groups: Optional[list[int]] = None,
) -> dict:
    """Counts of tickets per (technician group, status bucket).

    Buckets: ``new``, ``assigned``, ``planned``, ``waiting``,
    ``solved_30d``. When ``groups`` is empty, returns a single global
    row with key ``"all"``.
    """
    cutoff_solved = _glpi_dt(_now_utc() - timedelta(days=30))
    status_specs: list[tuple[str, list[dict]]] = [
        ("new", [_crit(_F_STATUS, "equals", _STATUS_NEW)]),
        ("assigned", [_crit(_F_STATUS, "equals", _STATUS_ASSIGNED)]),
        ("planned", [_crit(_F_STATUS, "equals", _STATUS_PLANNED)]),
        ("waiting", [_crit(_F_STATUS, "equals", _STATUS_WAITING)]),
        ("solved_30d", [
            _crit(_F_STATUS, "equals", _STATUS_SOLVED),
            _crit(_F_SOLVEDATE, "morethan", cutoff_solved, link="AND"),
        ]),
    ]

    targets: list[tuple[str, list[dict]]]
    if groups:
        targets = [
            (str(gid), [_group_criterion(gid)]) for gid in groups
        ]
    else:
        targets = [("all", [])]

    out: list[dict[str, Any]] = []
    for label, base_crit in targets:
        counts: dict[str, Any] = {"group_id": label}
        for bucket, status_crit in status_specs:
            crit = list(base_crit) + status_crit
            counts[bucket] = await _count(client, criteria=crit)
        active = counts["new"] + counts["assigned"] + counts["planned"] + counts["waiting"]
        counts["total_active"] = active
        out.append(counts)

    return {"groups": out, "truncated": False}


async def by_technician(
    client: GLPIClient,
    *,
    groups: Optional[list[int]] = None,
    top_n: int = 20,
) -> dict:
    """Active workload (open statuses) per assigned technician."""
    criteria: list[dict] = list(_open_status_criteria())
    if groups:
        # Multiple groups: filter to first group only — flat criteria can't
        # express OR across groups without nested arrays. Callers wanting
        # per-group breakdown should request the view per group.
        criteria.append(_group_criterion(groups[0]))

    res = await _safe_search(
        client,
        criteria=criteria,
        forcedisplay=[2, _F_NAME, _F_STATUS, _F_TECHNICIAN, _F_DATE_CREATION, _F_DATE_MOD],
    )
    rows = res.get("data") or []
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    now = _now_utc()
    workload: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"active": 0, "waiting": 0, "oldest_days": None}
    )
    for row in rows:
        tech = _label(_row_field(row, _F_TECHNICIAN))
        status = _label(_row_field(row, _F_STATUS)).lower()
        workload[tech]["active"] += 1
        if "wait" in status or "pend" in status or "agua" in status:
            workload[tech]["waiting"] += 1
        created = _parse_dt(_row_field(row, _F_DATE_CREATION))
        if created:
            age = (now - created).days
            cur = workload[tech]["oldest_days"]
            if cur is None or age > cur:
                workload[tech]["oldest_days"] = age

    sorted_rows = sorted(
        workload.items(), key=lambda kv: kv[1]["active"], reverse=True
    )[:top_n]
    return {
        "technicians": [{"technician": k, **v} for k, v in sorted_rows],
        "groups": groups or [],
        "truncated": truncated,
    }


async def stalled(
    client: GLPIClient,
    *,
    groups: Optional[list[int]] = None,
    days_threshold: int = 7,
    top_n: int = 50,
) -> dict:
    """Open tickets with ``date_mod`` older than ``days_threshold`` days."""
    cutoff = _glpi_dt(_now_utc() - timedelta(days=days_threshold))
    criteria: list[dict] = list(_open_status_criteria())
    criteria.append(_crit(_F_DATE_MOD, "lessthan", cutoff))
    if groups:
        criteria.append(_group_criterion(groups[0]))

    res = await _safe_search(
        client,
        criteria=criteria,
        forcedisplay=[2, _F_NAME, _F_STATUS, _F_TECHNICIAN, _F_TECH_GROUP, _F_DATE_MOD],
        sort=_F_DATE_MOD,
        order="ASC",
    )
    rows = res.get("data") or []
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    now = _now_utc()
    enriched: list[dict] = []
    for row in rows:
        last = _parse_dt(_row_field(row, _F_DATE_MOD))
        idle_days = (now - last).days if last else None
        enriched.append({
            "id": _row_field(row, 2) or _row_field(row, _F_NAME),
            "subject": _row_field(row, _F_NAME),
            "status": _label(_row_field(row, _F_STATUS)),
            "technician": _label(_row_field(row, _F_TECHNICIAN)),
            "tech_group": _label(_row_field(row, _F_TECH_GROUP)),
            "last_updated": _row_field(row, _F_DATE_MOD),
            "idle_days": idle_days,
        })
    enriched.sort(key=lambda r: r["idle_days"] or 0, reverse=True)
    return {
        "tickets": enriched[:top_n],
        "threshold_days": days_threshold,
        "truncated": truncated,
    }


async def sla_breaches(
    client: GLPIClient,
    *,
    groups: Optional[list[int]] = None,
    window_days: int = 7,
    sla_field_id: int = 18,
) -> dict:
    """Open tickets with the SLA target date past or within ``window_days``.

    GLPI's *time_to_resolve* field is normally searchOption ``18`` in a
    default install — the parameter ``sla_field_id`` lets operators
    override that for customised installs.
    """
    now = _now_utc()
    soon = now + timedelta(days=window_days)
    criteria: list[dict] = list(_open_status_criteria())
    criteria.append(_crit(sla_field_id, "lessthan", _glpi_dt(soon)))
    if groups:
        criteria.append(_group_criterion(groups[0]))

    res = await _safe_search(
        client,
        criteria=criteria,
        forcedisplay=[2, _F_NAME, _F_STATUS, _F_TECHNICIAN, sla_field_id],
        sort=sla_field_id,
        order="ASC",
    )
    rows = res.get("data") or []
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    breached: list[dict] = []
    near: list[dict] = []
    for row in rows:
        due_raw = _row_field(row, sla_field_id)
        due = _parse_dt(due_raw)
        if not due:
            continue
        item = {
            "id": _row_field(row, 2),
            "subject": _row_field(row, _F_NAME),
            "status": _label(_row_field(row, _F_STATUS)),
            "technician": _label(_row_field(row, _F_TECHNICIAN)),
            "due": due_raw,
            "hours_remaining": int((due - now).total_seconds() / 3600),
        }
        (breached if due <= now else near).append(item)

    breached.sort(key=lambda r: r["hours_remaining"])
    near.sort(key=lambda r: r["hours_remaining"])
    return {
        "breached": breached,
        "near_breach": near,
        "window_days": window_days,
        "sla_field_id": sla_field_id,
        "truncated": truncated,
    }


async def top_requesters(
    client: GLPIClient,
    *,
    groups: Optional[list[int]] = None,
    days: int = 30,
    top_n: int = 20,
) -> dict:
    """Top requesters by ticket count over the last ``days`` days."""
    cutoff = _glpi_dt(_now_utc() - timedelta(days=days))
    criteria: list[dict] = [_crit(_F_DATE_CREATION, "morethan", cutoff)]
    if groups:
        criteria.append(_group_criterion(groups[0]))

    res = await _safe_search(
        client,
        criteria=criteria,
        forcedisplay=[2, _F_REQUESTER, _F_DATE_CREATION],
    )
    rows = res.get("data") or []
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    counter: Counter[str] = Counter()
    for row in rows:
        counter[_label(_row_field(row, _F_REQUESTER))] += 1

    return {
        "requesters": [
            {"requester": k, "count": v} for k, v in counter.most_common(top_n)
        ],
        "days": days,
        "total_tickets": sum(counter.values()),
        "truncated": truncated,
    }


async def mean_ttr(
    client: GLPIClient,
    *,
    groups: Optional[list[int]] = None,
    days: int = 30,
) -> dict:
    """Mean and p95 time-to-resolve per technician group over ``days``."""
    cutoff = _glpi_dt(_now_utc() - timedelta(days=days))
    criteria: list[dict] = [
        _crit(_F_STATUS, "equals", _STATUS_SOLVED),
        _crit(_F_SOLVEDATE, "morethan", cutoff),
    ]
    if groups:
        criteria.append(_group_criterion(groups[0]))

    res = await _safe_search(
        client,
        criteria=criteria,
        forcedisplay=[2, _F_DATE_CREATION, _F_SOLVEDATE, _F_TECH_GROUP],
    )
    rows = res.get("data") or []
    truncated = len(rows) >= _MAX_TICKETS_PER_VIEW

    per_group: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        created = _parse_dt(_row_field(row, _F_DATE_CREATION))
        resolved = _parse_dt(_row_field(row, _F_SOLVEDATE))
        if not (created and resolved and resolved > created):
            continue
        hours = (resolved - created).total_seconds() / 3600.0
        group = _label(_row_field(row, _F_TECH_GROUP))
        per_group[group].append(hours)

    out = []
    for group, samples in sorted(per_group.items()):
        samples_sorted = sorted(samples)
        n = len(samples_sorted)
        mean = sum(samples_sorted) / n if n else 0.0
        p95 = samples_sorted[max(0, int(n * 0.95) - 1)] if n else 0.0
        out.append({
            "group": group,
            "tickets_resolved": n,
            "mean_hours": round(mean, 2),
            "p95_hours": round(p95, 2),
        })
    return {"groups": out, "days": days, "truncated": truncated}


async def trend(
    client: GLPIClient,
    *,
    groups: Optional[list[int]] = None,
    days: int = 14,
    granularity: str = "day",
) -> dict:
    """Daily/weekly trend of created vs. solved tickets.

    Issues two bounded searches (created-window and solved-window) and
    merges the resulting buckets, since GLPI's flat criteria array can't
    express ``Created OR Solvedate``.
    """
    if granularity not in {"day", "week"}:
        granularity = "day"
    cutoff = _now_utc() - timedelta(days=days)
    cutoff_str = _glpi_dt(cutoff)

    base_filter: list[dict] = []
    if groups:
        base_filter.append(_group_criterion(groups[0]))

    created_crit = list(base_filter) + [_crit(_F_DATE_CREATION, "morethan", cutoff_str)]
    solved_crit = list(base_filter) + [
        _crit(_F_STATUS, "equals", _STATUS_SOLVED),
        _crit(_F_SOLVEDATE, "morethan", cutoff_str),
    ]

    created_res = await _safe_search(
        client,
        criteria=created_crit,
        forcedisplay=[2, _F_DATE_CREATION],
    )
    solved_res = await _safe_search(
        client,
        criteria=solved_crit,
        forcedisplay=[2, _F_SOLVEDATE],
    )
    created_rows = created_res.get("data") or []
    solved_rows = solved_res.get("data") or []
    truncated = (
        len(created_rows) >= _MAX_TICKETS_PER_VIEW
        or len(solved_rows) >= _MAX_TICKETS_PER_VIEW
    )

    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"created": 0, "solved": 0})

    def _bucket_key(dt: datetime) -> str:
        if granularity == "week":
            year, week, _ = dt.isocalendar()
            return f"{year}-W{week:02d}"
        return dt.strftime("%Y-%m-%d")

    for row in created_rows:
        dt = _parse_dt(_row_field(row, _F_DATE_CREATION))
        if dt and dt >= cutoff:
            buckets[_bucket_key(dt)]["created"] += 1
    for row in solved_rows:
        dt = _parse_dt(_row_field(row, _F_SOLVEDATE))
        if dt and dt >= cutoff:
            buckets[_bucket_key(dt)]["solved"] += 1

    series = [
        {"bucket": k, "created": v["created"], "solved": v["solved"]}
        for k, v in sorted(buckets.items())
    ]
    return {
        "series": series,
        "granularity": granularity,
        "days": days,
        "truncated": truncated,
    }


__all__ = [
    "GLPIError",
    "by_group_status",
    "by_technician",
    "stalled",
    "sla_breaches",
    "top_requesters",
    "mean_ttr",
    "trend",
]
