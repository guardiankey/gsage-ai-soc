"""gSage AI — GitLab write operations (HITL-gated).

All write operations require human approval — the LLM MUST populate
``params._approval_summary`` with a clear one-line summary of the action.

Actions
-------
create_issue    — create a new issue (or many via 'issues' array)
update_issue    — update an existing issue (or many via 'updates' array):
                  title, description, assignees, labels (replace/add/remove),
                  state, milestone, due_date, confidential
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
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({"create_issue", "update_issue", "add_comment", "close_issue"})

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
        "Create, update or comment on GitLab issues. Supports batch "
        "create (issues[]) and batch update (updates[]). Human approval "
        "required for all operations."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "gitlab"
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
                    "create_issue: create a new issue (or many via 'issues' array). "
                    "update_issue: update an existing issue (or many via 'updates' array) — "
                    "change title, description, assignees, labels, state, milestone, due_date. "
                    "add_comment: post a comment on an existing issue. "
                    "close_issue: close an issue by IID (shortcut for update_issue+state_event=close)."
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
                "description": "[update_issue, add_comment, close_issue] Issue IID (internal to project).",
            },
            # ── create_issue / update_issue — single ─────────────────────────────────
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": (
                    "[create_issue, update_issue] Issue title. Required for create_issue "
                    "when 'issues' array is not used; optional for update_issue."
                ),
            },
            "description": {
                "type": "string",
                "maxLength": 10000,
                "description": "[create_issue, update_issue] Issue description (Markdown supported).",
            },
            "assignee_usernames": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
                "description": (
                    "[create_issue, update_issue] GitLab usernames to assign the issue to. "
                    "On update_issue this REPLACES the current assignees. "
                    "Pass an empty array to remove all assignees. "
                    "User IDs are resolved automatically."
                ),
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "[create_issue, update_issue] Label names. On update_issue this "
                    "REPLACES all current labels (use 'add_labels'/'remove_labels' for "
                    "incremental changes). Pass an empty array to clear all labels."
                ),
            },
            "milestone_id": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "[create_issue, update_issue] Milestone ID. Pass 0 on update_issue "
                    "to remove the milestone."
                ),
            },
            "due_date": {
                "type": "string",
                "description": (
                    "[create_issue, update_issue] Due date in ISO 8601 format "
                    "(YYYY-MM-DD). Pass empty string on update_issue to clear."
                ),
            },
            # ── update_issue extras ─────────────────────────────────────────────
            "add_labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "[update_issue] Labels to ADD without affecting existing labels. "
                    "Mutually exclusive with 'labels' (which replaces)."
                ),
            },
            "remove_labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "[update_issue] Labels to REMOVE without affecting other labels."
                ),
            },
            "state_event": {
                "type": "string",
                "enum": ["close", "reopen"],
                "description": "[update_issue] State transition to apply.",
            },
            "confidential": {
                "type": "boolean",
                "description": "[update_issue] Mark issue as confidential (true) or public (false).",
            },
            # ── create_issue — batch ─────────────────────────────────────────
            "issues": {
                "type": "array",
                "minItems": 1,
                "maxItems": 25,
                "description": (
                    "[create_issue] Create multiple issues at once. "
                    "When provided, top-level title/description/etc. are ignored. "
                    "Each element follows the same fields as a single create_issue."
                ),
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                            "description": "Issue title.",
                        },
                        "description": {
                            "type": "string",
                            "maxLength": 10000,
                            "description": "Issue description (Markdown).",
                        },
                        "assignee_usernames": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 10,
                            "description": "GitLab usernames to assign.",
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Label names.",
                        },
                        "milestone_id": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Milestone ID.",
                        },
                        "due_date": {
                            "type": "string",
                            "description": "Due date (YYYY-MM-DD).",
                        },
                    },
                    "additionalProperties": False,
                },
            },
            # ── update_issue — batch ────────────────────────────────────────
            "updates": {
                "type": "array",
                "minItems": 1,
                "maxItems": 25,
                "description": (
                    "[update_issue] Update multiple issues at once. "
                    "When provided, top-level title/description/etc. are ignored. "
                    "Each element must include 'issue_iid' plus the fields to change."
                ),
                "items": {
                    "type": "object",
                    "required": ["issue_iid"],
                    "properties": {
                        "issue_iid": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Issue IID to update.",
                        },
                        "title": {"type": "string", "minLength": 1, "maxLength": 255},
                        "description": {"type": "string", "maxLength": 10000},
                        "assignee_usernames": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 10,
                            "description": "Replace assignees. Empty array clears.",
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Replace labels. Empty array clears.",
                        },
                        "add_labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Add labels without affecting existing.",
                        },
                        "remove_labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Remove labels without affecting others.",
                        },
                        "milestone_id": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Milestone ID. 0 to remove.",
                        },
                        "due_date": {"type": "string", "description": "YYYY-MM-DD; empty to clear."},
                        "state_event": {"type": "string", "enum": ["close", "reopen"]},
                        "confidential": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
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

        # Batch mode: 'issues' array takes precedence over flat params
        issue_list: list[dict] = params.get("issues") or []
        if not issue_list:
            # Single mode: build a one-element list from flat params
            issue_list = [{
                "title": _require(params, "title"),
                "description": params.get("description"),
                "assignee_usernames": params.get("assignee_usernames"),
                "labels": params.get("labels"),
                "milestone_id": params.get("milestone_id"),
                "due_date": params.get("due_date"),
            }]

        created: list[dict] = []
        for item in issue_list:
            title = (item.get("title") or "").strip()
            if not title:
                raise _ParamError("Each issue must have a non-empty 'title'.")

            payload: dict = {"title": title}
            if item.get("description"):
                payload["description"] = item["description"]
            if item.get("labels"):
                payload["labels"] = ",".join(item["labels"])
            if item.get("milestone_id"):
                payload["milestone_id"] = int(item["milestone_id"])
            if item.get("due_date"):
                payload["due_date"] = item["due_date"]

            # Resolve assignee usernames → IDs
            assignee_usernames: list[str] = item.get("assignee_usernames") or []
            if assignee_usernames:
                payload["assignee_ids"] = await _resolve_assignee_ids(
                    client, assignee_usernames
                )

            raw = await client.create_issue(project, payload)
            created.append({
                "issue_iid": raw.get("iid"),
                "issue_id": raw.get("id"),
                "title": raw.get("title"),
                "state": raw.get("state"),
                "web_url": raw.get("web_url"),
            })

        result: dict = {"project": project, "created_count": len(created), "created": created}
        # Convenience: flatten for single-issue case
        if len(created) == 1:
            result.update(created[0])
        return result

    async def _do_update_issue(
        self, client: "GitLabClient", ctx: AgentContext, params: dict
    ) -> dict:
        project = _require(params, "project")

        # Batch mode: 'updates' array takes precedence over flat params
        update_list: list[dict] = params.get("updates") or []
        if not update_list:
            # Single mode: top-level 'issue_iid' + edit fields
            iid = _require_int(params, "issue_iid")
            update_list = [{
                "issue_iid": iid,
                "title": params.get("title"),
                "description": params.get("description"),
                "assignee_usernames": params.get("assignee_usernames"),
                "labels": params.get("labels"),
                "add_labels": params.get("add_labels"),
                "remove_labels": params.get("remove_labels"),
                "milestone_id": params.get("milestone_id"),
                "due_date": params.get("due_date"),
                "state_event": params.get("state_event"),
                "confidential": params.get("confidential"),
            }]

        updated: list[dict] = []
        for item in update_list:
            iid = item.get("issue_iid")
            if not iid:
                raise _ParamError("Each update must include 'issue_iid'.")

            payload = await _build_update_payload(client, item)
            if not payload:
                raise _ParamError(
                    f"update_issue for iid={iid} requires at least one field to change."
                )

            raw = await client.update_issue(project, int(iid), payload)
            updated.append({
                "issue_iid": raw.get("iid"),
                "title": raw.get("title"),
                "state": raw.get("state"),
                "labels": raw.get("labels"),
                "assignees": [
                    a.get("username") for a in (raw.get("assignees") or [])
                ],
                "due_date": raw.get("due_date"),
                "milestone_id": (raw.get("milestone") or {}).get("id"),
                "updated_at": raw.get("updated_at"),
                "web_url": raw.get("web_url"),
            })

        result: dict = {"project": project, "updated_count": len(updated), "updated": updated}
        if len(updated) == 1:
            result.update(updated[0])
        return result

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


async def _resolve_assignee_ids(
    client: GitLabClient, usernames: list[str]
) -> list[int]:
    """Resolve a list of GitLab usernames to numeric user IDs."""
    resolved: list[int] = []
    for username in usernames:
        users = await client.get_paginated(
            "/users", {"username": username}, max_items=1
        )
        if not users:
            raise GitLabError(
                f"Could not find GitLab user with username '{username}'.",
                code="NOT_FOUND",
            )
        resolved.append(users[0]["id"])
    return resolved


async def _build_update_payload(client: GitLabClient, item: dict) -> dict:
    """Translate a single update item into a GitLab PUT /issues/:iid payload.

    - ``labels`` REPLACES (empty list clears).
    - ``add_labels`` / ``remove_labels`` are incremental.
    - ``assignee_usernames`` REPLACES (empty list clears via assignee_ids=[0]).
    - ``milestone_id == 0`` removes the milestone.
    - ``due_date == ""`` clears the due date.
    """
    payload: dict = {}

    if item.get("title") is not None:
        title = (item["title"] or "").strip()
        if title:
            payload["title"] = title

    if item.get("description") is not None:
        payload["description"] = item["description"]

    # Labels: replace vs add/remove are mutually exclusive in spirit but
    # GitLab accepts both in the same request — we pass through whatever is set.
    if item.get("labels") is not None:
        payload["labels"] = ",".join(item["labels"]) if item["labels"] else ""
    if item.get("add_labels"):
        payload["add_labels"] = ",".join(item["add_labels"])
    if item.get("remove_labels"):
        payload["remove_labels"] = ",".join(item["remove_labels"])

    # Assignees: replace
    if item.get("assignee_usernames") is not None:
        usernames = item["assignee_usernames"] or []
        if usernames:
            payload["assignee_ids"] = await _resolve_assignee_ids(client, usernames)
        else:
            # GitLab convention to clear assignees: assignee_ids=[0]
            payload["assignee_ids"] = [0]

    if item.get("milestone_id") is not None:
        payload["milestone_id"] = int(item["milestone_id"])

    if item.get("due_date") is not None:
        payload["due_date"] = item["due_date"]

    if item.get("state_event"):
        payload["state_event"] = item["state_event"]

    if item.get("confidential") is not None:
        payload["confidential"] = bool(item["confidential"])

    return payload
