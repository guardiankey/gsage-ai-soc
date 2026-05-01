"""gSage AI — RT write/admin operations (HITL-gated).

Single tool that dispatches to a write action against Request Tracker:
ticket lifecycle (create/update/comment/correspond/take/untake/steal),
links/merges, queue admin, user admin, and **fetch_attachment** which
downloads an RT attachment into MinIO and returns a signed download path.

All actions require human approval — the LLM **MUST** populate
``params._approval_summary`` with a concise human-readable summary
of what will happen.

Required permission: ``rt:write``.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult, _tool_session_ctx
from src.mcp_server.tools.soc.ticket.rt._client import (
    RT_CONFIG_DEFAULTS,
    RT_CONFIG_SCHEMA,
    RTClient,
    RTError,
    build_rt_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = {
    "create_ticket",
    "update_ticket",
    "comment",
    "correspond",
    "take",
    "untake",
    "steal",
    "merge",
    "manage_link",
    "bulk_create",
    "bulk_update",
    "queue_create",
    "queue_update",
    "queue_delete",
    "user_create",
    "user_update",
    "fetch_attachment",
}

# Hard caps for bulk operations.
_BULK_MAX = 25
_FETCH_ATT_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB

_LINK_TYPES = {"DependsOn", "DependedOnBy", "RefersTo", "ReferredToBy", "MemberOf", "HasMember"}
_LINK_OPS = {"add", "remove"}

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    """Return a filesystem-friendly version of *name*."""
    if not name:
        return "attachment"
    cleaned = _FILENAME_SAFE_RE.sub("_", name).strip("._")
    return cleaned or "attachment"


def _missing(field: str) -> ToolResult:  # pragma: no cover — helper
    raise NotImplementedError  # placeholder; real method on the tool


class RTManageTool(BaseTool):
    """Write/admin operations against RT.

    Action list (set ``params.action``):

    - ``create_ticket``: queue, subject, content, [content_type, requestor,
      cc, admin_cc, priority, owner, custom_fields].
    - ``update_ticket``: ticket_id + any RT field (subject, status, owner,
      queue, priority, due, custom_fields).
    - ``comment`` / ``correspond``: ticket_id, content, [content_type].
    - ``take`` / ``untake`` / ``steal``: ticket_id.
    - ``merge``: ticket_id (source), into_id (target).
    - ``manage_link``: ticket_id, link_type, target, op (add|remove).
    - ``bulk_create`` (≤25): tickets list of create_ticket payloads.
    - ``bulk_update`` (≤25): ticket_ids + fields.
    - ``queue_create`` / ``queue_update`` / ``queue_delete``: queue admin.
    - ``user_create`` / ``user_update``: user admin.
    - ``fetch_attachment``: ticket_id, attachment_id → downloads bytes
      into MinIO; returns ``file_id`` and ``download_path``. Filename
      pattern: ``rt_<ticket_id>_<original_filename>``.

    HITL: every call requires ``params._approval_summary`` with a clear
    one-line description of the operation.

    Permission: ``rt:write``.
    """

    name: ClassVar[str] = "rt_manage"
    config_namespace: ClassVar[str] = "rt"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "RT write/admin operations: tickets, comments, links, merges, queues, "
        "users, bulk operations and attachment download. HITL-gated."
    )
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["rt:write"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_id": "ticket_id",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which RT operation to perform.",
            },
            # Common identifiers
            "ticket_id": {"type": "integer", "minimum": 1},
            "into_id": {"type": "integer", "minimum": 1},
            "attachment_id": {"type": "integer", "minimum": 1},
            "queue_id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            "user_id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            # Ticket fields
            "queue": {"type": "string"},
            "subject": {"type": "string"},
            "content": {"type": "string"},
            "content_type": {
                "type": "string",
                "enum": ["text/plain", "text/html"],
                "default": "text/plain",
            },
            "status": {"type": "string"},
            "owner": {"type": "string"},
            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
            "due": {"type": "string"},
            "requestor": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "cc": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "admin_cc": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "custom_fields": {
                "type": "object",
                "additionalProperties": True,
                "description": "Map of CF name → value.",
            },
            # Link
            "link_type": {"type": "string", "enum": sorted(_LINK_TYPES)},
            "target": {
                "oneOf": [{"type": "integer", "minimum": 1}, {"type": "string"}],
                "description": "Target ticket id or URI for link operations.",
            },
            "op": {"type": "string", "enum": sorted(_LINK_OPS)},
            # Bulk
            "tickets": {
                "type": "array",
                "maxItems": _BULK_MAX,
                "items": {"type": "object"},
                "description": "Array of create_ticket payloads (max 25).",
            },
            "ticket_ids": {
                "type": "array",
                "maxItems": _BULK_MAX,
                "items": {"type": "integer", "minimum": 1},
            },
            "fields": {
                "type": "object",
                "additionalProperties": True,
                "description": "Field map for bulk_update.",
            },
            # Queue admin
            "queue_payload": {
                "type": "object",
                "additionalProperties": True,
                "description": "Queue create/update payload.",
            },
            # User admin
            "user_payload": {
                "type": "object",
                "additionalProperties": True,
                "description": "User create/update payload.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = RT_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = RT_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = params.get("action")
        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )

        try:
            async with build_rt_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, agent_context, params)
        except RTError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("MISSING_PARAM", str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("rt_manage(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data},
            execution_time_ms=elapsed,
        )

    # ── Action handlers ────────────────────────────────────────────────

    async def _do_create_ticket(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        queue = _require(params, "queue")
        subject = _require(params, "subject")
        content = _require(params, "content")
        kwargs = _ticket_payload(params)
        ticket_id = await client.create_ticket(
            queue=queue,
            subject=subject,
            content=content,
            content_type=params.get("content_type") or "text/plain",
            **kwargs,
        )
        return {"ticket_id": ticket_id}

    async def _do_update_ticket(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        fields = _ticket_payload(params, include_subject=True)
        if not fields:
            raise _ParamError("update_ticket requires at least one field to change.")
        ok = await client.edit_ticket(ticket_id, **fields)
        return {"ticket_id": ticket_id, "updated": bool(ok), "fields": list(fields)}

    async def _do_comment(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        return await self._comment_or_correspond(client, params, reply=False)

    async def _do_correspond(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        return await self._comment_or_correspond(client, params, reply=True)

    @staticmethod
    async def _comment_or_correspond(
        client: RTClient, params: dict, *, reply: bool
    ) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        content = _require(params, "content")
        ct = params.get("content_type") or "text/plain"
        if reply:
            ok = await client.reply(ticket_id, content=content, content_type=ct)
        else:
            ok = await client.comment(ticket_id, content=content, content_type=ct)
        return {"ticket_id": ticket_id, "ok": bool(ok)}

    async def _do_take(self, client: RTClient, ctx: AgentContext, params: dict) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        ok = await client.take(ticket_id)
        return {"ticket_id": ticket_id, "ok": bool(ok)}

    async def _do_untake(self, client: RTClient, ctx: AgentContext, params: dict) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        ok = await client.untake(ticket_id)
        return {"ticket_id": ticket_id, "ok": bool(ok)}

    async def _do_steal(self, client: RTClient, ctx: AgentContext, params: dict) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        ok = await client.steal(ticket_id)
        return {"ticket_id": ticket_id, "ok": bool(ok)}

    async def _do_merge(self, client: RTClient, ctx: AgentContext, params: dict) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        into_id = _require_int(params, "into_id")
        ok = await client.merge_ticket(ticket_id, into_id)
        return {"ticket_id": ticket_id, "into_id": into_id, "ok": bool(ok)}

    async def _do_manage_link(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        link_type = _require(params, "link_type")
        target = params.get("target")
        if target in (None, ""):
            raise _ParamError("manage_link requires 'target' (ticket id or URI).")
        op = params.get("op") or "add"
        if op not in _LINK_OPS:
            raise _ParamError(f"manage_link op must be one of {sorted(_LINK_OPS)}.")
        ok = await client.edit_link(
            ticket_id, link_type, str(target), delete=(op == "remove")
        )
        return {
            "ticket_id": ticket_id,
            "link_type": link_type,
            "target": target,
            "op": op,
            "ok": bool(ok),
        }

    async def _do_bulk_create(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        items = params.get("tickets") or []
        if not items:
            raise _ParamError("bulk_create requires non-empty 'tickets' list.")
        if len(items) > _BULK_MAX:
            raise _ParamError(f"bulk_create accepts at most {_BULK_MAX} tickets per call.")
        results: list[dict] = []
        for i, payload in enumerate(items):
            try:
                queue = _require(payload, "queue")
                subject = _require(payload, "subject")
                content = _require(payload, "content")
                kwargs = _ticket_payload(payload)
                tid = await client.create_ticket(
                    queue=queue,
                    subject=subject,
                    content=content,
                    content_type=payload.get("content_type") or "text/plain",
                    **kwargs,
                )
                results.append({"index": i, "ok": True, "ticket_id": tid})
            except (RTError, _ParamError) as exc:
                results.append({"index": i, "ok": False, "error": str(exc)})
        return {
            "total": len(items),
            "succeeded": sum(1 for r in results if r["ok"]),
            "results": results,
        }

    async def _do_bulk_update(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        ids = params.get("ticket_ids") or []
        if not ids:
            raise _ParamError("bulk_update requires non-empty 'ticket_ids' list.")
        if len(ids) > _BULK_MAX:
            raise _ParamError(f"bulk_update accepts at most {_BULK_MAX} tickets per call.")
        fields = params.get("fields") or {}
        if not fields:
            raise _ParamError("bulk_update requires non-empty 'fields' object.")
        results: list[dict] = []
        for tid in ids:
            try:
                ok = await client.edit_ticket(int(tid), **fields)
                results.append({"ticket_id": int(tid), "ok": bool(ok)})
            except RTError as exc:
                results.append({"ticket_id": int(tid), "ok": False, "error": str(exc)})
        return {
            "total": len(ids),
            "succeeded": sum(1 for r in results if r["ok"]),
            "results": results,
        }

    async def _do_queue_create(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        payload = params.get("queue_payload") or {}
        if not payload.get("Name") and not payload.get("name"):
            raise _ParamError("queue_create requires queue_payload.Name.")
        rt_obj = client._rt_or_error()  # noqa: SLF001
        result = await rt_obj.create_queue(**payload)  # type: ignore[attr-defined]
        return {"queue": result}

    async def _do_queue_update(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        queue_id = params.get("queue_id")
        if queue_id in (None, ""):
            raise _ParamError("queue_update requires 'queue_id'.")
        payload = params.get("queue_payload") or {}
        if not payload:
            raise _ParamError("queue_update requires non-empty 'queue_payload'.")
        rt_obj = client._rt_or_error()  # noqa: SLF001
        result = await rt_obj.edit_queue(queue_id, **payload)  # type: ignore[attr-defined]
        return {"queue_id": queue_id, "ok": bool(result)}

    async def _do_queue_delete(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        queue_id = params.get("queue_id")
        if queue_id in (None, ""):
            raise _ParamError("queue_delete requires 'queue_id'.")
        rt_obj = client._rt_or_error()  # noqa: SLF001
        # RT REST 2.0 does not truly delete queues; the lib disables them.
        result = await rt_obj.delete_queue(queue_id)  # type: ignore[attr-defined]
        return {"queue_id": queue_id, "ok": bool(result)}

    async def _do_user_create(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        payload = params.get("user_payload") or {}
        if not payload.get("Name") and not payload.get("name"):
            raise _ParamError("user_create requires user_payload.Name.")
        rt_obj = client._rt_or_error()  # noqa: SLF001
        result = await rt_obj.create_user(**payload)  # type: ignore[attr-defined]
        return {"user": result}

    async def _do_user_update(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        user_id = params.get("user_id")
        if user_id in (None, ""):
            raise _ParamError("user_update requires 'user_id'.")
        payload = params.get("user_payload") or {}
        if not payload:
            raise _ParamError("user_update requires non-empty 'user_payload'.")
        rt_obj = client._rt_or_error()  # noqa: SLF001
        result = await rt_obj.edit_user(user_id, **payload)  # type: ignore[attr-defined]
        return {"user_id": user_id, "ok": bool(result)}

    async def _do_fetch_attachment(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        attachment_id = _require_int(params, "attachment_id")
        att = await client.get_attachment(attachment_id)

        # RT returns Content as base64 (Content + ContentEncoding=base64).
        raw_content = att.get("Content") or att.get("content")
        encoding = (att.get("ContentEncoding") or "base64").lower()
        if not raw_content:
            raise RTError(
                f"Attachment {attachment_id} has no Content payload.",
                code="RT_ERROR",
            )
        try:
            data = base64.b64decode(raw_content) if encoding == "base64" else (
                raw_content.encode("utf-8") if isinstance(raw_content, str) else raw_content
            )
        except Exception as exc:  # noqa: BLE001
            raise RTError(
                f"Failed to decode attachment {attachment_id}: {exc}",
                code="RT_ERROR",
            ) from exc

        if len(data) > _FETCH_ATT_MAX_BYTES:
            raise RTError(
                f"Attachment {attachment_id} exceeds the {_FETCH_ATT_MAX_BYTES} byte cap.",
                code="INVALID_PARAMS",
            )

        original = att.get("Filename") or att.get("filename") or f"att_{attachment_id}"
        filename = f"rt_{ticket_id}_{_safe_filename(original)}"
        content_type = att.get("ContentType") or att.get("content_type") or "application/octet-stream"

        session = _tool_session_ctx.get()
        if session is None:
            raise RTError(
                "fetch_attachment requires an active DB session (tool runtime context).",
                code="INTERNAL_ERROR",
            )

        stored = await self._store_file(
            data=data,
            filename=filename,
            content_type=content_type,
            agent_context=ctx,
            session=session,
            description=f"Attachment from RT ticket #{ticket_id}",
        )
        if stored is None:
            raise RTError(
                "Failed to persist the attachment in the file store.",
                code="INTERNAL_ERROR",
            )
        return {
            "ticket_id": ticket_id,
            "attachment_id": attachment_id,
            "file": stored,
        }


# ── Param helpers ──────────────────────────────────────────────────────


class _ParamError(Exception):
    """Raised by per-action handlers to signal a missing/invalid parameter."""


def _require(params: dict, key: str) -> str:
    val = params.get(key)
    if val in (None, "", []):
        raise _ParamError(f"'{key}' is required.")
    return str(val)


def _require_int(params: dict, key: str) -> int:
    val = params.get(key)
    if val in (None, ""):
        raise _ParamError(f"'{key}' is required.")
    try:
        i = int(val)
    except (TypeError, ValueError) as exc:
        raise _ParamError(f"'{key}' must be an integer.") from exc
    if i <= 0:
        raise _ParamError(f"'{key}' must be > 0.")
    return i


def _ticket_payload(params: dict, *, include_subject: bool = False) -> dict[str, Any]:
    """Build a python-rt-friendly kwargs dict from common ticket fields."""
    out: dict[str, Any] = {}
    if include_subject and params.get("subject"):
        out["Subject"] = params["subject"]
    if params.get("status"):
        out["Status"] = params["status"]
    if params.get("queue"):
        out["Queue"] = params["queue"]
    if params.get("owner"):
        out["Owner"] = params["owner"]
    if params.get("priority") is not None:
        out["Priority"] = int(params["priority"])
    if params.get("due"):
        out["Due"] = params["due"]
    for src, dst in (("requestor", "Requestor"), ("cc", "Cc"), ("admin_cc", "AdminCc")):
        v = params.get(src)
        if v:
            out[dst] = v if isinstance(v, list) else [v]
    cf = params.get("custom_fields") or {}
    if cf:
        out["CustomFields"] = cf
    return out
