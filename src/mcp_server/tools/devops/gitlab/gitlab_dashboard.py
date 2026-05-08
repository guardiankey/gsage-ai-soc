"""gSage AI — GitLab managerial dashboards.

Aggregates GitLab project data into managerial views: issue summary,
workload per assignee, overdue issues, stale issues, milestone progress,
time tracking and creation/closure trend.

This is a read-only tool — no human approval is required.

Each view returns a ``truncated`` flag (when applicable) so the agent
knows whether the underlying fetch hit the per-view hard cap.

Required permission: ``gitlab:read``.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.gitlab._client import (
    GITLAB_CONFIG_DEFAULTS,
    GITLAB_CONFIG_SCHEMA,
    GitLabClient,
    GitLabError,
    build_gitlab_client,
    encode_project_id,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_VIEWS = frozenset(
    {
        "summary",
        "by_assignee",
        "overdue",
        "stale",
        "milestones",
        "time_tracking",
        "trend",
        "labels_distribution",
        "merge_requests",
    }
)

# Per-view hard caps on number of issues fetched (X-Next-Page paged).
# GitLab returns up to 100 per page; we cap to keep memory/time bounded.
_FETCH_CAP = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # GitLab returns ISO 8601 with 'Z' or with offset
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _today_utc_date() -> str:
    return _now_utc().date().isoformat()


def _seconds_to_h(secs: int) -> float:
    return round(secs / 3600.0, 2) if secs else 0.0


def _bucket_key(dt: datetime, granularity: str) -> str:
    if granularity == "week":
        # ISO week: Monday-based (YYYY-Www)
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return dt.date().isoformat()


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class GitLabDashboardTool(BaseTool):
    """Compute managerial views over GitLab project data.

    Pick a ``view``; supply ``project`` (numeric ID or namespace/path).
    Each view has its own optional parameters.

    Views:
    - ``summary``: overall counts (open/closed/total) plus quick stats
      (overdue count, unassigned count, top assignees, top labels, recent
      activity within ``days``).
    - ``by_assignee``: workload per assignee — open count, overdue count,
      avg age in days, total time spent (hours).  Top N.
    - ``overdue``: list of open issues whose ``due_date`` is past today.
    - ``stale``: list of open issues not updated in the last
      ``days_threshold`` days.
    - ``milestones``: per-milestone progress (open vs closed counts,
      due_date, % complete) for ``state``.
    - ``time_tracking``: aggregate ``time_estimate`` vs ``total_time_spent``
      across issues, plus list of issues over budget.
    - ``trend``: created vs closed counts per day or week within
      ``days`` window.
    - ``labels_distribution``: label usage count among open issues.
    - ``merge_requests``: open MR workload by assignee/author and stale MRs.

    Permission: ``gitlab:read``.
    """

    name: ClassVar[str] = "gitlab_dashboard"
    config_namespace: ClassVar[str] = "gitlab"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Managerial GitLab dashboards: project summary, per-assignee workload, "
        "overdue, stale, milestones, time tracking and creation/closure trend."
    )
    category: ClassVar[str] = "devops"
    permissions: ClassVar[list[str]] = ["gitlab:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 90
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {
        "view": "view",
        "project": "project",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["view", "project"],
        "properties": {
            "view": {
                "type": "string",
                "enum": sorted(_VIEWS),
                "description": "Which managerial aggregation to compute.",
            },
            "project": {
                "type": "string",
                "description": (
                    "Project identifier: numeric ID (e.g. '42') or "
                    "namespace/path (e.g. 'mygroup/myrepo')."
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile to use.  Omit to use the "
                    "'default' profile."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Row cap for views that produce a leaderboard (default: 10).",
            },
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "description": (
                    "Look-back window in days. Used by 'summary' (recent "
                    "activity) and 'trend' (default: 30 / 14)."
                ),
            },
            "days_threshold": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "description": "Idle days threshold for the 'stale' view (default: 14).",
            },
            "granularity": {
                "type": "string",
                "enum": ["day", "week"],
                "description": "Bucket size for the 'trend' view (default: day).",
            },
            "state": {
                "type": "string",
                "enum": ["active", "closed", "all"],
                "description": "[milestones] Filter milestones by state (default: active).",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional labels filter (AND semantics) applied before "
                    "aggregation. Useful to scope a view to a single team or "
                    "category."
                ),
            },
            "milestone": {
                "type": "string",
                "description": (
                    "Optional milestone title to restrict the aggregation to."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _FETCH_CAP,
                "description": (
                    f"Hard cap on issues to fetch per view (default and max: {_FETCH_CAP}). "
                    "Lower this to reduce latency for very large projects."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = GITLAB_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = GITLAB_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ──────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        view = params.get("view") or ""
        project = (params.get("project") or "").strip()

        if view not in _VIEWS:
            return self._failure(
                "INVALID_PARAMS",
                f"view must be one of {sorted(_VIEWS)}; got {view!r}.",
            )
        if not project:
            return self._failure(
                "INVALID_PARAMS", "'project' is required."
            )

        try:
            async with build_gitlab_client(config) as client:
                handler = getattr(self, f"_view_{view}")
                data = await handler(client, project, params)
        except GitLabError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("gitlab_dashboard(%s): unexpected error", view)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "view": view,
                "project": project,
                "generated_at": _now_utc().isoformat(),
                **data,
            },
            execution_time_ms=elapsed,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Shared fetch helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _fetch_issues(
        self,
        client: GitLabClient,
        project: str,
        *,
        state: str = "all",
        params: Optional[dict] = None,
        max_results: int = _FETCH_CAP,
        extra_filters: Optional[dict] = None,
    ) -> list[dict]:
        """Fetch issues with shared filter logic (labels, milestone)."""
        pid = encode_project_id(project)
        q: dict = {"state": state, "scope": "all"}
        if extra_filters:
            q.update(extra_filters)
        if params:
            if params.get("labels"):
                q["labels"] = ",".join(params["labels"])
            if params.get("milestone"):
                q["milestone"] = params["milestone"]
        issues = await client.get_paginated(
            f"/projects/{pid}/issues", q, max_items=max_results
        )
        return issues

    # ─────────────────────────────────────────────────────────────────────────
    # Views
    # ─────────────────────────────────────────────────────────────────────────

    async def _view_summary(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        days = int(params.get("days") or 30)
        top_n = int(params.get("top_n") or 10)
        max_results = int(params.get("max_results") or _FETCH_CAP)
        cutoff = _now_utc() - timedelta(days=days)
        today = _now_utc().date()

        issues = await self._fetch_issues(
            client, project, state="all", params=params, max_results=max_results
        )

        opened = [i for i in issues if i.get("state") == "opened"]
        closed = [i for i in issues if i.get("state") == "closed"]

        overdue_count = sum(
            1 for i in opened
            if i.get("due_date") and i["due_date"] < today.isoformat()
        )
        unassigned_count = sum(
            1 for i in opened if not (i.get("assignees") or [])
        )

        # Top assignees among open issues
        assignee_counter: Counter = Counter()
        for i in opened:
            for a in i.get("assignees") or []:
                if a.get("username"):
                    assignee_counter[a["username"]] += 1

        # Top labels among open issues
        label_counter: Counter = Counter()
        for i in opened:
            for label in i.get("labels") or []:
                label_counter[label] += 1

        # Recent activity counts
        created_recent = sum(
            1 for i in issues
            if (dt := _parse_iso(i.get("created_at"))) and dt >= cutoff
        )
        closed_recent = sum(
            1 for i in closed
            if (dt := _parse_iso(i.get("closed_at"))) and dt >= cutoff
        )

        return {
            "totals": {
                "open": len(opened),
                "closed": len(closed),
                "total": len(issues),
                "overdue": overdue_count,
                "unassigned": unassigned_count,
            },
            f"recent_{days}d": {
                "created": created_recent,
                "closed": closed_recent,
                "net": created_recent - closed_recent,
            },
            "top_assignees": [
                {"username": u, "open_count": c}
                for u, c in assignee_counter.most_common(top_n)
            ],
            "top_labels": [
                {"label": lb, "open_count": c}
                for lb, c in label_counter.most_common(top_n)
            ],
            "fetched": len(issues),
            "truncated": len(issues) >= max_results,
        }

    async def _view_by_assignee(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        top_n = int(params.get("top_n") or 20)
        max_results = int(params.get("max_results") or _FETCH_CAP)
        today = _now_utc().date()
        now = _now_utc()

        issues = await self._fetch_issues(
            client, project, state="opened", params=params, max_results=max_results
        )

        agg: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "open_count": 0,
                "overdue_count": 0,
                "age_sum_days": 0.0,
                "time_spent_seconds": 0,
            }
        )

        for i in issues:
            assignees = i.get("assignees") or []
            if not assignees:
                key = "(unassigned)"
                buckets = [agg[key]]
            else:
                buckets = [agg[a.get("username", "(unknown)")] for a in assignees]

            created = _parse_iso(i.get("created_at"))
            age_days = ((now - created).total_seconds() / 86400.0) if created else 0.0
            is_overdue = bool(
                i.get("due_date") and i["due_date"] < today.isoformat()
            )
            time_spent = int(((i.get("time_stats") or {}).get("total_time_spent") or 0))

            for b in buckets:
                b["open_count"] += 1
                b["age_sum_days"] += age_days
                if is_overdue:
                    b["overdue_count"] += 1
                b["time_spent_seconds"] += time_spent

        rows: list[dict] = []
        for username, b in agg.items():
            cnt = max(b["open_count"], 1)
            rows.append({
                "username": username,
                "open_count": b["open_count"],
                "overdue_count": b["overdue_count"],
                "avg_age_days": round(b["age_sum_days"] / cnt, 1),
                "time_spent_hours": _seconds_to_h(b["time_spent_seconds"]),
            })
        rows.sort(key=lambda r: r["open_count"], reverse=True)
        rows = rows[:top_n]

        return {
            "rows": rows,
            "fetched": len(issues),
            "truncated": len(issues) >= max_results,
        }

    async def _view_overdue(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        top_n = int(params.get("top_n") or 50)
        max_results = int(params.get("max_results") or _FETCH_CAP)
        today_iso = _today_utc_date()

        issues = await self._fetch_issues(
            client, project, state="opened", params=params,
            max_results=max_results,
            extra_filters={"due_date_before": today_iso},
        )

        rows = []
        for i in issues:
            due = i.get("due_date")
            if not due or due >= today_iso:
                continue
            try:
                days_late = (
                    _now_utc().date() - datetime.fromisoformat(due).date()
                ).days
            except Exception:
                days_late = 0
            rows.append({
                "iid": i.get("iid"),
                "title": i.get("title"),
                "due_date": due,
                "days_late": days_late,
                "assignees": [
                    a.get("username") for a in (i.get("assignees") or [])
                ],
                "labels": i.get("labels"),
                "milestone": (i.get("milestone") or {}).get("title"),
                "web_url": i.get("web_url"),
            })

        rows.sort(key=lambda r: r["days_late"], reverse=True)
        rows = rows[:top_n]

        return {
            "rows": rows,
            "count": len(rows),
            "fetched": len(issues),
            "truncated": len(issues) >= max_results,
        }

    async def _view_stale(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        top_n = int(params.get("top_n") or 50)
        max_results = int(params.get("max_results") or _FETCH_CAP)
        threshold = int(params.get("days_threshold") or 14)
        cutoff = _now_utc() - timedelta(days=threshold)

        issues = await self._fetch_issues(
            client, project, state="opened", params=params,
            max_results=max_results,
        )

        rows = []
        now = _now_utc()
        for i in issues:
            updated = _parse_iso(i.get("updated_at"))
            if not updated or updated > cutoff:
                continue
            idle_days = int((now - updated).total_seconds() / 86400.0)
            rows.append({
                "iid": i.get("iid"),
                "title": i.get("title"),
                "updated_at": i.get("updated_at"),
                "idle_days": idle_days,
                "assignees": [
                    a.get("username") for a in (i.get("assignees") or [])
                ],
                "labels": i.get("labels"),
                "web_url": i.get("web_url"),
            })

        rows.sort(key=lambda r: r["idle_days"], reverse=True)
        rows = rows[:top_n]

        return {
            "rows": rows,
            "count": len(rows),
            "days_threshold": threshold,
            "fetched": len(issues),
            "truncated": len(issues) >= max_results,
        }

    async def _view_milestones(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        max_results = int(params.get("max_results") or _FETCH_CAP)
        state = params.get("state") or "active"

        pid = encode_project_id(project)
        ms = await client.get_paginated(
            f"/projects/{pid}/milestones",
            {"state": state},
            max_items=max_results,
        )

        today = _now_utc().date()
        rows = []
        for m in ms:
            opened = int(m.get("open_issues_count") or 0)
            closed = int(m.get("closed_issues_count") or 0)
            total = opened + closed
            pct = round((closed / total) * 100.0, 1) if total else 0.0
            due = m.get("due_date")
            days_to_due = None
            overdue = False
            if due:
                try:
                    due_d = datetime.fromisoformat(due).date()
                    days_to_due = (due_d - today).days
                    overdue = days_to_due < 0 and opened > 0
                except Exception:
                    pass

            rows.append({
                "id": m.get("id"),
                "iid": m.get("iid"),
                "title": m.get("title"),
                "state": m.get("state"),
                "due_date": due,
                "days_to_due": days_to_due,
                "is_overdue": overdue,
                "open_issues": opened,
                "closed_issues": closed,
                "total_issues": total,
                "completion_pct": pct,
                "web_url": m.get("web_url"),
            })

        return {
            "rows": rows,
            "count": len(rows),
            "fetched": len(ms),
        }

    async def _view_time_tracking(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        top_n = int(params.get("top_n") or 20)
        max_results = int(params.get("max_results") or _FETCH_CAP)

        issues = await self._fetch_issues(
            client, project, state="all", params=params, max_results=max_results
        )

        total_estimate = 0
        total_spent = 0
        with_estimate = 0
        with_spent = 0
        over_budget: list[dict] = []
        per_user_spent: dict[str, float] = defaultdict(float)  # by assignee

        for i in issues:
            ts = i.get("time_stats") or {}
            est = int(ts.get("time_estimate") or 0)
            spent = int(ts.get("total_time_spent") or 0)
            total_estimate += est
            total_spent += spent
            if est > 0:
                with_estimate += 1
            if spent > 0:
                with_spent += 1

            if est > 0 and spent > est:
                over_budget.append({
                    "iid": i.get("iid"),
                    "title": i.get("title"),
                    "state": i.get("state"),
                    "estimate_hours": _seconds_to_h(est),
                    "spent_hours": _seconds_to_h(spent),
                    "over_pct": round(((spent - est) / est) * 100.0, 1),
                    "assignees": [
                        a.get("username") for a in (i.get("assignees") or [])
                    ],
                    "web_url": i.get("web_url"),
                })

            # Distribute spent time across assignees as a heuristic
            assignees = i.get("assignees") or []
            if spent and assignees:
                share = spent / len(assignees)
                for a in assignees:
                    if u := a.get("username"):
                        per_user_spent[u] += share

        over_budget.sort(key=lambda r: r["over_pct"], reverse=True)

        return {
            "totals": {
                "estimate_hours": _seconds_to_h(total_estimate),
                "spent_hours": _seconds_to_h(total_spent),
                "remaining_hours": _seconds_to_h(max(0, total_estimate - total_spent)),
                "issues_with_estimate": with_estimate,
                "issues_with_spent": with_spent,
                "issues_total": len(issues),
            },
            "top_users_by_time_spent": [
                {"username": u, "spent_hours": round(secs / 3600.0, 2)}
                for u, secs in sorted(
                    per_user_spent.items(), key=lambda kv: kv[1], reverse=True
                )[:top_n]
            ],
            "over_budget": over_budget[:top_n],
            "over_budget_count": len(over_budget),
            "fetched": len(issues),
            "truncated": len(issues) >= max_results,
            "note": (
                "Per-user time spent is estimated by dividing each issue's "
                "total_time_spent equally among its assignees. GitLab does not "
                "expose authoritative per-user time tracking via REST."
            ),
        }

    async def _view_trend(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        days = int(params.get("days") or 14)
        granularity = params.get("granularity") or "day"
        max_results = int(params.get("max_results") or _FETCH_CAP)
        cutoff = _now_utc() - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        # Fetch issues created OR closed in the window. We use updated_after
        # as a proxy and then filter client-side.
        issues = await self._fetch_issues(
            client, project, state="all", params=params,
            max_results=max_results,
            extra_filters={"updated_after": cutoff_iso},
        )

        created_buckets: Counter = Counter()
        closed_buckets: Counter = Counter()

        for i in issues:
            created = _parse_iso(i.get("created_at"))
            if created and created >= cutoff:
                created_buckets[_bucket_key(created, granularity)] += 1
            closed = _parse_iso(i.get("closed_at"))
            if closed and closed >= cutoff:
                closed_buckets[_bucket_key(closed, granularity)] += 1

        all_keys = sorted(set(created_buckets) | set(closed_buckets))
        series = [
            {
                "bucket": k,
                "created": created_buckets.get(k, 0),
                "closed": closed_buckets.get(k, 0),
                "net": created_buckets.get(k, 0) - closed_buckets.get(k, 0),
            }
            for k in all_keys
        ]

        return {
            "granularity": granularity,
            "days": days,
            "series": series,
            "totals": {
                "created": sum(created_buckets.values()),
                "closed": sum(closed_buckets.values()),
                "net": sum(created_buckets.values()) - sum(closed_buckets.values()),
            },
            "fetched": len(issues),
            "truncated": len(issues) >= max_results,
        }

    async def _view_labels_distribution(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        top_n = int(params.get("top_n") or 30)
        max_results = int(params.get("max_results") or _FETCH_CAP)

        issues = await self._fetch_issues(
            client, project, state="opened", params=params, max_results=max_results
        )
        counter: Counter = Counter()
        for i in issues:
            for lb in i.get("labels") or []:
                counter[lb] += 1

        return {
            "rows": [
                {"label": lb, "open_count": c}
                for lb, c in counter.most_common(top_n)
            ],
            "distinct_labels": len(counter),
            "fetched": len(issues),
            "truncated": len(issues) >= max_results,
        }

    async def _view_merge_requests(
        self, client: GitLabClient, project: str, params: dict
    ) -> dict:
        top_n = int(params.get("top_n") or 20)
        max_results = int(params.get("max_results") or _FETCH_CAP)
        threshold = int(params.get("days_threshold") or 7)
        cutoff = _now_utc() - timedelta(days=threshold)

        pid = encode_project_id(project)
        mrs = await client.get_paginated(
            f"/projects/{pid}/merge_requests",
            {"state": "opened", "scope": "all"},
            max_items=max_results,
        )

        by_assignee: Counter = Counter()
        by_author: Counter = Counter()
        stale_rows: list[dict] = []

        for mr in mrs:
            assignees = mr.get("assignees") or []
            if not assignees:
                by_assignee["(unassigned)"] += 1
            else:
                for a in assignees:
                    if u := a.get("username"):
                        by_assignee[u] += 1

            if author_user := (mr.get("author") or {}).get("username"):
                by_author[author_user] += 1

            updated = _parse_iso(mr.get("updated_at"))
            if updated and updated < cutoff:
                stale_rows.append({
                    "iid": mr.get("iid"),
                    "title": mr.get("title"),
                    "source_branch": mr.get("source_branch"),
                    "target_branch": mr.get("target_branch"),
                    "updated_at": mr.get("updated_at"),
                    "idle_days": int(
                        (_now_utc() - updated).total_seconds() / 86400.0
                    ),
                    "assignees": [a.get("username") for a in assignees],
                    "author": (mr.get("author") or {}).get("username"),
                    "web_url": mr.get("web_url"),
                })

        stale_rows.sort(key=lambda r: r["idle_days"], reverse=True)

        return {
            "open_total": len(mrs),
            "by_assignee": [
                {"username": u, "open_count": c}
                for u, c in by_assignee.most_common(top_n)
            ],
            "by_author": [
                {"username": u, "open_count": c}
                for u, c in by_author.most_common(top_n)
            ],
            "stale": {
                "days_threshold": threshold,
                "rows": stale_rows[:top_n],
                "count": len(stale_rows),
            },
            "fetched": len(mrs),
            "truncated": len(mrs) >= max_results,
        }
