"""gSage AI — GitLab read-only operations.

Provides access to GitLab project/issue information without requiring human
approval.  All operations are read-only (GET requests only).

Actions
-------
list_projects        — search for projects the token can access
get_project          — get details of a single project
list_users           — search for users by name or username
get_user             — get a single user by ID
list_project_members — list members of a project
list_branches        — list branches in a project repository
list_issues          — search issues with rich filters
get_issue            — get a single issue by IID
list_issue_notes     — list comments on an issue
list_labels          — list project labels
list_milestones      — list project milestones
list_commits         — list commits in a project, optionally filtered
list_merge_requests  — list merge requests in a project

``project`` field accepts either a numeric project ID (e.g. ``42``) or a
namespace/path string (e.g. ``"mygroup/myproject"``).

Required permission: ``gitlab:read``.
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

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

_MAX_RESULTS = 500
_DEFAULT_RESULTS = 20

_ACTIONS = frozenset(
    {
        "list_projects",
        "get_project",
        "list_users",
        "get_user",
        "list_project_members",
        "list_branches",
        "list_issues",
        "get_issue",
        "list_issue_notes",
        "list_labels",
        "list_milestones",
        "list_commits",
        "list_merge_requests",
    }
)

# ---------------------------------------------------------------------------
# Response slimming helpers — keep only the fields agents actually use
# ---------------------------------------------------------------------------


def _slim_project(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "path_with_namespace": p.get("path_with_namespace"),
        "description": p.get("description"),
        "web_url": p.get("web_url"),
        "visibility": p.get("visibility"),
        "default_branch": p.get("default_branch"),
        "archived": p.get("archived"),
        "last_activity_at": p.get("last_activity_at"),
        "namespace": (p.get("namespace") or {}).get("full_path"),
        "open_issues_count": p.get("open_issues_count"),
    }


def _slim_user(u: dict) -> dict:
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "name": u.get("name"),
        "state": u.get("state"),
        "web_url": u.get("web_url"),
        "avatar_url": u.get("avatar_url"),
    }


def _slim_member(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "username": m.get("username"),
        "name": m.get("name"),
        "access_level": m.get("access_level"),
        "expires_at": m.get("expires_at"),
    }


def _slim_branch(b: dict) -> dict:
    commit = b.get("commit") or {}
    return {
        "name": b.get("name"),
        "protected": b.get("protected"),
        "merged": b.get("merged"),
        "default": b.get("default"),
        "last_commit_id": commit.get("id"),
        "last_commit_title": commit.get("title"),
        "last_commit_date": commit.get("committed_date"),
        "last_commit_author": (commit.get("author_name") or commit.get("committer_name")),
    }


def _slim_issue(i: dict) -> dict:
    assignees = i.get("assignees") or []
    milestone = i.get("milestone") or {}
    return {
        "id": i.get("id"),
        "iid": i.get("iid"),
        "project_id": i.get("project_id"),
        "title": i.get("title"),
        "state": i.get("state"),
        "description": (i.get("description") or "")[:500] or None,
        "labels": i.get("labels"),
        "assignees": [{"username": a.get("username"), "name": a.get("name")} for a in assignees],
        "author": {
            "username": (i.get("author") or {}).get("username"),
            "name": (i.get("author") or {}).get("name"),
        },
        "milestone": {"id": milestone.get("id"), "title": milestone.get("title")} if milestone else None,
        "due_date": i.get("due_date"),
        "created_at": i.get("created_at"),
        "updated_at": i.get("updated_at"),
        "closed_at": i.get("closed_at"),
        "web_url": i.get("web_url"),
        "user_notes_count": i.get("user_notes_count"),
        "has_tasks": i.get("has_tasks"),
    }


def _slim_note(n: dict) -> dict:
    return {
        "id": n.get("id"),
        "author": {"username": (n.get("author") or {}).get("username")},
        "body": (n.get("body") or "")[:500],
        "created_at": n.get("created_at"),
        "system": n.get("system"),
    }


def _slim_label(lb: dict) -> dict:
    return {
        "id": lb.get("id"),
        "name": lb.get("name"),
        "color": lb.get("color"),
        "description": lb.get("description"),
        "open_issues_count": lb.get("open_issues_count"),
    }


def _slim_milestone(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "iid": m.get("iid"),
        "title": m.get("title"),
        "description": (m.get("description") or "")[:200] or None,
        "state": m.get("state"),
        "due_date": m.get("due_date"),
        "start_date": m.get("start_date"),
        "web_url": m.get("web_url"),
        "open_issues_count": m.get("open_issues_count"),
        "closed_issues_count": m.get("closed_issues_count"),
    }


def _slim_commit(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "short_id": c.get("short_id"),
        "title": c.get("title"),
        "author_name": c.get("author_name"),
        "author_email": c.get("author_email"),
        "authored_date": c.get("authored_date"),
        "message": (c.get("message") or "")[:200],
        "web_url": c.get("web_url"),
    }


def _slim_mr(mr: dict) -> dict:
    assignees = mr.get("assignees") or []
    milestone = mr.get("milestone") or {}
    return {
        "id": mr.get("id"),
        "iid": mr.get("iid"),
        "title": mr.get("title"),
        "state": mr.get("state"),
        "description": (mr.get("description") or "")[:300] or None,
        "labels": mr.get("labels"),
        "assignees": [{"username": a.get("username")} for a in assignees],
        "author": {"username": (mr.get("author") or {}).get("username")},
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "milestone": {"id": milestone.get("id"), "title": milestone.get("title")} if milestone else None,
        "created_at": mr.get("created_at"),
        "updated_at": mr.get("updated_at"),
        "merged_at": mr.get("merged_at"),
        "web_url": mr.get("web_url"),
        "user_notes_count": mr.get("user_notes_count"),
    }


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class GitLabReadTool(BaseTool):
    """Read-only access to GitLab projects, issues and related resources.

    Supports multi-profile: set ``params.profile`` to use a non-default
    GSageToolConfig profile (e.g. ``"self-hosted"``).  Defaults to the
    ``"default"`` profile.

    Permission: ``gitlab:read``.
    """

    name: ClassVar[str] = "gitlab_read"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read GitLab projects, issues, users, branches, commits, milestones "
        "and merge requests. No human approval required."
    )
    category: ClassVar[str] = "devops"
    permissions: ClassVar[list[str]] = ["gitlab:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "project": "project",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which read operation to perform.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile to use.  Omit to use the "
                    "'default' profile (most common).  Useful when the org "
                    "has multiple GitLab instances configured."
                ),
            },
            # ── Entity identifiers ──────────────────────────────────────────
            "project": {
                "type": "string",
                "description": (
                    "[Most actions] Project identifier: numeric ID (e.g. '42') "
                    "or namespace/path (e.g. 'mygroup/myrepo')."
                ),
            },
            "user_id": {
                "type": "integer",
                "minimum": 1,
                "description": "[get_user] GitLab user ID.",
            },
            "issue_iid": {
                "type": "integer",
                "minimum": 1,
                "description": "[get_issue, list_issue_notes] Issue IID (internal to project).",
            },
            # ── Pagination / result size ────────────────────────────────────
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "description": (
                    f"Maximum number of items to return (hard cap {_MAX_RESULTS}). "
                    f"Default: {_DEFAULT_RESULTS}."
                ),
            },
            # ── list_projects / list_users ──────────────────────────────────
            "search": {
                "type": "string",
                "description": (
                    "[list_projects, list_users, list_branches, list_labels, "
                    "list_milestones, list_project_members] Free-text search string."
                ),
            },
            "visibility": {
                "type": "string",
                "enum": ["public", "internal", "private"],
                "description": "[list_projects] Filter by visibility.",
            },
            "membership": {
                "type": "boolean",
                "description": (
                    "[list_projects] When true, list only projects where "
                    "the token owner is a member."
                ),
            },
            "archived": {
                "type": "boolean",
                "description": "[list_projects] Include/exclude archived projects.",
            },
            # ── list_issues filters ─────────────────────────────────────────
            "state": {
                "type": "string",
                "enum": ["opened", "closed", "all"],
                "description": (
                    "[list_issues, list_merge_requests, list_milestones] "
                    "Filter by state."
                ),
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "[list_issues, list_merge_requests] Filter by label names "
                    "(all labels must match — AND semantics)."
                ),
            },
            "assignee": {
                "type": "string",
                "description": (
                    "[list_issues, list_merge_requests] Filter by assignee "
                    "username (e.g. 'joao.silva').  Use the GitLab username, "
                    "not the display name."
                ),
            },
            "author": {
                "type": "string",
                "description": (
                    "[list_issues, list_merge_requests] Filter by author username."
                ),
            },
            "milestone": {
                "type": "string",
                "description": (
                    "[list_issues, list_merge_requests] Filter by milestone title. "
                    "Use '%23upcoming' for the next open milestone or '%23started' "
                    "for started milestones."
                ),
            },
            "due_before": {
                "type": "string",
                "description": (
                    "[list_issues] Issues due before this date (ISO 8601, e.g. "
                    "'2026-05-31')."
                ),
            },
            "due_after": {
                "type": "string",
                "description": "[list_issues] Issues due after this date (ISO 8601).",
            },
            "scope": {
                "type": "string",
                "enum": ["created_by_me", "assigned_to_me", "all"],
                "description": "[list_issues] Scope of issues to return.",
            },
            "issue_search": {
                "type": "string",
                "description": "[list_issues] Full-text search in issue title and description.",
            },
            "sort": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "[list_issues, list_merge_requests] Sort direction.",
            },
            "order_by": {
                "type": "string",
                "description": (
                    "[list_issues] Order by field: created_at, updated_at, "
                    "priority, due_date, relative_position, label_priority, "
                    "milestone_due, popularity, weight."
                ),
            },
            # ── list_commits ────────────────────────────────────────────────
            "ref": {
                "type": "string",
                "description": (
                    "[list_commits] Branch, tag or commit SHA to list commits for "
                    "(defaults to project default branch)."
                ),
            },
            "since": {
                "type": "string",
                "description": (
                    "[list_commits] Only commits after this datetime "
                    "(ISO 8601, e.g. '2026-01-01T00:00:00Z')."
                ),
            },
            "until": {
                "type": "string",
                "description": "[list_commits] Only commits before this datetime (ISO 8601).",
            },
            "referenced_issue_iid": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "[list_commits] When set, only return commits whose message "
                    "contains a reference to issue #<iid>  (e.g. 'Closes #42', "
                    "'Refs #42').  Filtered client-side after fetching."
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
        action = params.get("action", "")

        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )

        max_results = min(
            int(params.get("max_results") or _DEFAULT_RESULTS),
            _MAX_RESULTS,
        )

        try:
            async with build_gitlab_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, max_results)
        except GitLabError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("gitlab_read(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(data={"action": action, **data}, execution_time_ms=elapsed)

    # ── Action handlers ──────────────────────────────────────────────────────

    async def _do_list_projects(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        p: dict = {}
        if params.get("search"):
            p["search"] = params["search"]
        if params.get("visibility"):
            p["visibility"] = params["visibility"]
        if params.get("membership") is not None:
            p["membership"] = str(params["membership"]).lower()
        if params.get("archived") is not None:
            p["archived"] = str(params["archived"]).lower()
        p["order_by"] = "last_activity_at"
        p["sort"] = "desc"

        raw = await client.get_paginated("/projects", p, max_items=max_results)
        return {"items": [_slim_project(r) for r in raw], "count": len(raw)}

    async def _do_get_project(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = params.get("project") or ""
        if not project:
            raise GitLabError("'project' is required for get_project.", code="INVALID_PARAMS")
        raw = await client.get_project(project)
        return {"project": _slim_project(raw)}

    async def _do_list_users(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        p: dict = {}
        if params.get("search"):
            p["search"] = params["search"]
        p["active"] = "true"
        raw = await client.get_paginated("/users", p, max_items=max_results)
        return {"items": [_slim_user(r) for r in raw], "count": len(raw)}

    async def _do_get_user(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        user_id = params.get("user_id")
        if not user_id:
            raise GitLabError("'user_id' is required for get_user.", code="INVALID_PARAMS")
        raw = await client.get_user(int(user_id))
        return {"user": _slim_user(raw)}

    async def _do_list_project_members(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        pid = encode_project_id(project)
        p: dict = {}
        if params.get("search"):
            p["query"] = params["search"]
        raw = await client.get_paginated(
            f"/projects/{pid}/members/all", p, max_items=max_results
        )
        return {"items": [_slim_member(r) for r in raw], "count": len(raw)}

    async def _do_list_branches(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        pid = encode_project_id(project)
        p: dict = {}
        if params.get("search"):
            p["search"] = params["search"]
        raw = await client.get_paginated(
            f"/projects/{pid}/repository/branches", p, max_items=max_results
        )
        return {"items": [_slim_branch(r) for r in raw], "count": len(raw)}

    async def _do_list_issues(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        pid = encode_project_id(project)
        p: dict = {}
        if params.get("state"):
            p["state"] = params["state"]
        else:
            p["state"] = "opened"
        if params.get("assignee"):
            p["assignee_username"] = params["assignee"]
        if params.get("author"):
            p["author_username"] = params["author"]
        if params.get("labels"):
            p["labels"] = ",".join(params["labels"])
        if params.get("milestone"):
            p["milestone"] = params["milestone"]
        if params.get("due_before"):
            p["due_date_before"] = params["due_before"]
        if params.get("due_after"):
            p["due_date_after"] = params["due_after"]
        if params.get("issue_search"):
            p["search"] = params["issue_search"]
        if params.get("order_by"):
            p["order_by"] = params["order_by"]
        if params.get("sort"):
            p["sort"] = params["sort"]
        if params.get("scope"):
            p["scope"] = params["scope"]

        raw = await client.get_paginated(
            f"/projects/{pid}/issues", p, max_items=max_results
        )
        return {"items": [_slim_issue(r) for r in raw], "count": len(raw)}

    async def _do_get_issue(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        issue_iid = _require_issue_iid(params)
        raw = await client.get_issue(project, issue_iid)
        return {"issue": _slim_issue(raw)}

    async def _do_list_issue_notes(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        issue_iid = _require_issue_iid(params)
        pid = encode_project_id(project)
        raw = await client.get_paginated(
            f"/projects/{pid}/issues/{issue_iid}/notes",
            {"sort": "asc"},
            max_items=max_results,
        )
        return {"items": [_slim_note(r) for r in raw], "count": len(raw)}

    async def _do_list_labels(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        pid = encode_project_id(project)
        p: dict = {}
        if params.get("search"):
            p["search"] = params["search"]
        raw = await client.get_paginated(
            f"/projects/{pid}/labels", p, max_items=max_results
        )
        return {"items": [_slim_label(r) for r in raw], "count": len(raw)}

    async def _do_list_milestones(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        pid = encode_project_id(project)
        p: dict = {}
        if params.get("state"):
            p["state"] = params["state"]
        if params.get("search"):
            p["search"] = params["search"]
        raw = await client.get_paginated(
            f"/projects/{pid}/milestones", p, max_items=max_results
        )
        return {"items": [_slim_milestone(r) for r in raw], "count": len(raw)}

    async def _do_list_commits(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        pid = encode_project_id(project)
        p: dict = {}
        if params.get("ref"):
            p["ref_name"] = params["ref"]
        if params.get("since"):
            p["since"] = params["since"]
        if params.get("until"):
            p["until"] = params["until"]

        # Fetch slightly more when doing issue-reference filter (client-side)
        ref_iid = params.get("referenced_issue_iid")
        fetch_cap = min(max_results * 5, _MAX_RESULTS) if ref_iid else max_results

        raw = await client.get_paginated(
            f"/projects/{pid}/repository/commits", p, max_items=fetch_cap
        )

        if ref_iid:
            patterns = (f"#{ref_iid}", f"!{ref_iid}")
            raw = [
                c for c in raw
                if any(pat in (c.get("message") or "") for pat in patterns)
            ]
            raw = raw[:max_results]

        return {"items": [_slim_commit(r) for r in raw], "count": len(raw)}

    async def _do_list_merge_requests(
        self, client: "GitLabClient", params: dict, max_results: int
    ) -> dict:

        project = _require_project(params)
        pid = encode_project_id(project)
        p: dict = {}
        if params.get("state"):
            p["state"] = params["state"]
        else:
            p["state"] = "opened"
        if params.get("assignee"):
            p["assignee_username"] = params["assignee"]
        if params.get("author"):
            p["author_username"] = params["author"]
        if params.get("labels"):
            p["labels"] = ",".join(params["labels"])
        if params.get("milestone"):
            p["milestone"] = params["milestone"]
        if params.get("search"):
            p["search"] = params["search"]
        if params.get("sort"):
            p["sort"] = params["sort"]
        raw = await client.get_paginated(
            f"/projects/{pid}/merge_requests", p, max_items=max_results
        )
        return {"items": [_slim_mr(r) for r in raw], "count": len(raw)}


# ---------------------------------------------------------------------------
# Param validators
# ---------------------------------------------------------------------------


def _require_project(params: dict) -> str:
    project = (params.get("project") or "").strip()
    if not project:
        raise GitLabError(
            "'project' is required for this action.", code="INVALID_PARAMS"
        )
    return project


def _require_issue_iid(params: dict) -> int:
    iid = params.get("issue_iid")
    if not iid:
        raise GitLabError(
            "'issue_iid' is required for this action.", code="INVALID_PARAMS"
        )
    return int(iid)
