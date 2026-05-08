"""gSage AI — GitLab write operations (HITL-gated).

All write operations require human approval — the LLM MUST populate
``params._approval_summary`` with a clear one-line summary of the action.

Actions
-------
create_issue    — create a new issue in a project
add_comment     — add a comment (note) to an existing issue
close_issue     — close an issue (sets state_event=close)

``project`` field accepts either a numeric project ID or a
namespace/path string (e.g. ``"mygroup/myrepo"``).

Required permission: ``gitlab:write``.
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

_ACTIONS = frozenset({"create_issue", "add_comment", "close_issue"})

# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class GitLabManageTool(BaseTool):
    """Write operations against GitLab (HITL-gated).

    Every action requires human approval.  The LLM **MUST** set
    ``params._approval_summary`` to a concise human-readable description
    of the operation (e.g. "Create issue 'Fix null pointer' in project
    backend with label 'bug' assigned to joao").

    Supports multi-profile: set ``params.profile`` to use a non-default
    GSageToolConfig profile.  Defaults to the ``"default"`` profile.

    Permission: ``gitlab:write``.
    """

    name: ClassVar[str] = "gitlab_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Create or update GitLab issues: create_issue, add_comment, close_issue. "
        "Human approval required for all operations."
    )
    category: ClassVar[str] = "devops"
    permissions: ClassVar[list[str]] = ["gitlab:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "project": "project",
        "issue_iid": "issue_iid",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": (
                    "create_issue: create a new issue in the project. "
                    "add_comment: post a comment on an existing issue. "
                    "close_issue: close an issue by IID."
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile to use.  Omit to use the "
                    "'default' profile."
                ),
            },
            # ── Shared ──────────────────────────────────────────────────────
            "project": {
                "type": "string",
                "description": (
                    "Project identifier: numeric ID (e.g. '42') or "
                    "namespace/path (e.g. 'mygroup/myrepo')."
                ),
            },
            "issue_iid": {
                "type": "integer",
                "minimum": 1,
                "description": "[add_comment, close_issue] Issue IID (internal to project).",
            },
            # ── create_issue ─────────────────────────────────────────────────
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "[create_issue] Issue title (required).",
            },
            "description": {
                "type": "string",
                "maxLength": 10000,
                "description": "[create_issue] Issue description (Markdown supported).",
            },
            "assignee_usernames": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
                "description": (
                    "[create_issue] List of GitLab usernames to assign the issue to.  "
                    "User IDs are resolved automatically."
                ),
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "[create_issue] Label names to attach to the issue.",
            },
            "milestone_id": {
                "type": "integer",
                "minimum": 1,
                "description": "[create_issue] Milestone ID to associate with the issue.",
            },
            "due_date": {
                "type": "string",
                "description": "[create_issue] Due date in ISO 8601 format (YYYY-MM-DD).",
            },
            # ── add_comment ──────────────────────────────────────────────────
            "body": {
                "type": "string",
                "minLength": 1,
                "maxLength": 10000,
                "description": "[add_comment] Comment body (Markdown supported).",
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

        try:
            async with build_gitlab_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, agent_context, params)
        except GitLabError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("MISSING_PARAM", str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("gitlab_manage(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(data={"action": action, **data}, execution_time_ms=elapsed)

    # ── Action handlers ──────────────────────────────────────────────────────

    async def _do_create_issue(
        self, client: "GitLabClient", ctx: AgentContext, params: dict
    ) -> dict:

        project = _require(params, "project")
        title = _require(params, "title")
        pid = encode_project_id(project)

        payload: dict = {"title": title}

        if params.get("description"):
            payload["description"] = params["description"]
        if params.get("labels"):
            payload["labels"] = ",".join(params["labels"])
        if params.get("milestone_id"):
            payload["milestone_id"] = int(params["milestone_id"])
        if params.get("due_date"):
            payload["due_date"] = params["due_date"]

        # Resolve assignee usernames → IDs
        assignee_usernames: list[str] = params.get("assignee_usernames") or []
        if assignee_usernames:
            resolved_ids = []
            for username in assignee_usernames:
                users = await client.get_paginated(
                    "/users", {"username": username}, max_items=1
                )
                if users:
                    resolved_ids.append(users[0]["id"])
                else:
                    raise GitLabError(
                        f"Could not find GitLab user with username '{username}'.",
                        code="NOT_FOUND",
                    )
            payload["assignee_ids"] = resolved_ids

        raw = await client.create_issue(project, payload)

        return {
            "issue_iid": raw.get("iid"),
            "issue_id": raw.get("id"),
            "title": raw.get("title"),
            "state": raw.get("state"),
            "web_url": raw.get("web_url"),
            "project": project,
        }

    async def _do_add_comment(
        self, client: "GitLabClient", ctx: AgentContext, params: dict
    ) -> dict:

        project = _require(params, "project")
        issue_iid = _require_int(params, "issue_iid")
        body = _require(params, "body")

        raw = await client.create_note(project, issue_iid, body)

        return {
            "note_id": raw.get("id"),
            "issue_iid": issue_iid,
            "project": project,
            "author": (raw.get("author") or {}).get("username"),
            "created_at": raw.get("created_at"),
        }

    async def _do_close_issue(
        self, client: "GitLabClient", ctx: AgentContext, params: dict
    ) -> dict:

        project = _require(params, "project")
        issue_iid = _require_int(params, "issue_iid")

        raw = await client.update_issue(
            project, issue_iid, {"state_event": "close"}
        )

        return {
            "issue_iid": issue_iid,
            "project": project,
            "state": raw.get("state"),
            "closed_at": raw.get("closed_at"),
            "web_url": raw.get("web_url"),
        }


# ---------------------------------------------------------------------------
# Param helpers
# ---------------------------------------------------------------------------


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = (params.get(field) or "").strip()
    if not val:
        raise _ParamError(f"'{field}' is required for this action.")
    return val


def _require_int(params: dict, field: str) -> int:
    val = params.get(field)
    if not val:
        raise _ParamError(f"'{field}' is required for this action.")
    return int(val)
