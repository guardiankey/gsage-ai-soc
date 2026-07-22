"""gSage AI — RT write/admin operations (HITL-gated).

Single tool that dispatches to a write action against Request Tracker:
ticket lifecycle (create/update/comment/correspond/take/untake/steal),
links/merges, queue admin and user admin.

For each of ``create_ticket``, ``comment`` and ``correspond`` the caller
may pass ``attachment_file_ids`` — a list of conversation-scoped file
IDs (uploaded by the user or produced by another tool). Each file is
loaded from the gSage file store and forwarded to RT as a binary
attachment.

To *download* an existing RT attachment, use the read-only tool
``rt_get_ticket`` with ``action='fetch_attachment'`` — it does not
require human approval.

All write actions require human approval — the LLM **MUST** populate
``params._approval_summary`` with a concise human-readable summary
of what will happen.

Required permission: ``rt:write``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.rt._client import (
    RT_CONFIG_DEFAULTS,
    RT_CONFIG_SCHEMA,
    RTClient,
    RTError,
    build_rt_client,
)
from src.shared.security.context import AgentContext

try:
    from rt.rest2 import Attachment as _RTAttachment  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    _RTAttachment = None  # type: ignore[assignment,misc]

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
}

# Hard caps for bulk operations.
_BULK_MAX = 25

# Hard caps for outbound RT attachments (per call).
_ATTACHMENT_MAX_COUNT = 10
_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB per file
_ATTACHMENT_TOTAL_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB combined per call

_LINK_TYPES = {"DependsOn", "DependedOnBy", "RefersTo", "ReferredToBy", "MemberOf", "HasMember"}
_LINK_OPS = {"add", "remove"}


def _missing(field: str) -> ToolResult:  # pragma: no cover — helper
    raise NotImplementedError  # placeholder; real method on the tool


class RTManageTool(BaseTool):
    """Write/admin operations against RT.

    Action list (set ``params.action``):

    - ``create_ticket``: queue, subject, content, [content_type, requestor,
      cc, admin_cc, priority, owner, custom_fields, attachment_file_ids].
    - ``update_ticket``: ticket_id + any RT field (subject, status, owner,
      queue, priority, due, custom_fields).
    - ``comment`` / ``correspond``: ticket_id, content, [content_type,
      attachment_file_ids].
    - ``take`` / ``untake`` / ``steal``: ticket_id.
    - ``merge``: ticket_id (source), into_id (target).
    - ``manage_link``: ticket_id, link_type, target, op (add|remove).
    - ``bulk_create`` (≤25): tickets list of create_ticket payloads.
    - ``bulk_update`` (≤25): ticket_ids + fields.
    - ``queue_create`` / ``queue_update`` / ``queue_delete``: queue admin.
    - ``user_create`` / ``user_update``: user admin.

    To download an RT attachment, use the read-only tool ``rt_get_ticket``
    with ``action='fetch_attachment'`` — no HITL required.

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
            # Outbound attachments (conversation-scoped files)
            "attachment_file_ids": {
                "type": "array",
                "maxItems": _ATTACHMENT_MAX_COUNT,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "List of gSage file IDs (conversation-scoped) to attach "
                    "to the ticket. Applies to create_ticket, comment and "
                    f"correspond. Max {_ATTACHMENT_MAX_COUNT} files, "
                    f"{_ATTACHMENT_MAX_BYTES} bytes each, "
                    f"{_ATTACHMENT_TOTAL_MAX_BYTES} bytes combined."
                ),
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
        attachments, att_meta, att_skipped = await self._load_rt_attachments(
            params.get("attachment_file_ids") or [], ctx
        )
        ticket_id = await client.create_ticket(
            queue=queue,
            subject=subject,
            content=content,
            content_type=params.get("content_type") or "text/plain",
            attachments=attachments or None,
            **kwargs,
        )
        out: dict[str, Any] = {"ticket_id": ticket_id}
        if att_meta:
            out["attachments"] = att_meta
        if att_skipped:
            out["attachments_skipped"] = att_skipped
        return out

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
        return await self._comment_or_correspond(client, ctx, params, reply=False)

    async def _do_correspond(
        self, client: RTClient, ctx: AgentContext, params: dict
    ) -> dict:
        return await self._comment_or_correspond(client, ctx, params, reply=True)

    async def _comment_or_correspond(
        self,
        client: RTClient,
        ctx: AgentContext,
        params: dict,
        *,
        reply: bool,
    ) -> dict:
        ticket_id = _require_int(params, "ticket_id")
        content = _require(params, "content")
        ct = params.get("content_type") or "text/plain"
        attachments, att_meta, att_skipped = await self._load_rt_attachments(
            params.get("attachment_file_ids") or [], ctx
        )
        if reply:
            ok = await client.reply(
                ticket_id,
                content=content,
                content_type=ct,
                attachments=attachments or None,
            )
        else:
            ok = await client.comment(
                ticket_id,
                content=content,
                content_type=ct,
                attachments=attachments or None,
            )
        out: dict[str, Any] = {"ticket_id": ticket_id, "ok": bool(ok)}
        if att_meta:
            out["attachments"] = att_meta
        if att_skipped:
            out["attachments_skipped"] = att_skipped
        return out

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
        payload = dict(params.get("queue_payload") or {})
        # The rt library's create_queue(name, **kwargs) expects ``name`` as a
        # positional argument and maps it to the RT API ``Name`` field.
        # Extract it from the payload so it is not passed twice.
        queue_name = payload.pop("Name", None) or payload.pop("name", None)
        if not queue_name:
            raise _ParamError("queue_create requires queue_payload.Name.")
        rt_obj = client._rt_or_error()  # noqa: SLF001
        result = await rt_obj.create_queue(queue_name, **payload)  # type: ignore[attr-defined]
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
        payload = dict(params.get("user_payload") or {})
        # The rt library's create_user(user_name, email_address, **kwargs)
        # expects ``user_name`` and ``email_address`` as positional args and
        # maps them to the RT API ``Name`` and ``EmailAddress`` fields.
        # Extract both from the payload so they are not passed twice.
        user_name = payload.pop("Name", None) or payload.pop("name", None)
        email = payload.pop("EmailAddress", None) or payload.pop("email_address", None)
        if not user_name:
            raise _ParamError("user_create requires user_payload.Name.")
        if not email:
            raise _ParamError("user_create requires user_payload.EmailAddress.")
        rt_obj = client._rt_or_error()  # noqa: SLF001
        result = await rt_obj.create_user(user_name, email, **payload)  # type: ignore[attr-defined]
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

    # ── Outbound attachment helper ─────────────────────────────────────

    async def _load_rt_attachments(
        self,
        file_ids: list[str],
        ctx: AgentContext,
    ) -> tuple[list, list[dict], list[dict]]:
        """Load conversation-scoped files and convert them to RT Attachments.

        Honours the same 3-way scope ACL as :meth:`BaseTool._load_file`
        (organization / department / user). Files that exceed the per-file
        cap, are truncated, are not found, or push the call past the
        combined-size cap are reported under ``skipped``.

        Returns
        -------
        (rt_attachments, meta, skipped)
            * ``rt_attachments``: list of ``rt.rest2.Attachment`` ready for
              the python-rt client.
            * ``meta``: list of dicts with ``file_id``, ``filename``,
              ``content_type``, ``size_bytes`` for the attachments included.
            * ``skipped``: list of dicts with ``file_id`` and ``reason``.
        """
        rt_attachments: list = []
        meta: list[dict] = []
        skipped: list[dict] = []
        if not file_ids:
            return rt_attachments, meta, skipped

        if _RTAttachment is None:
            raise RTError(
                "python-rt is not installed; cannot attach files.",
                code="INTERNAL_ERROR",
            )

        if len(file_ids) > _ATTACHMENT_MAX_COUNT:
            for fid in file_ids[_ATTACHMENT_MAX_COUNT:]:
                skipped.append({"file_id": fid, "reason": "max_count_exceeded"})
            file_ids = file_ids[:_ATTACHMENT_MAX_COUNT]

        total_bytes = 0
        for fid in file_ids:
            loaded = await self._load_file(
                file_id=fid,
                org_id=str(ctx.org_id),
                user_id=str(ctx.user_id),
                dept_id=str(ctx.dept_id) if ctx.dept_id else None,
                max_bytes=_ATTACHMENT_MAX_BYTES,
            )
            if loaded is None:
                skipped.append({"file_id": fid, "reason": "not_found_or_denied"})
                continue
            if loaded.get("truncated"):
                skipped.append({
                    "file_id": fid,
                    "filename": loaded.get("filename"),
                    "reason": "exceeds_per_file_cap",
                })
                continue
            data: bytes = loaded["data"]
            if total_bytes + len(data) > _ATTACHMENT_TOTAL_MAX_BYTES:
                skipped.append({
                    "file_id": fid,
                    "filename": loaded.get("filename"),
                    "reason": "exceeds_total_cap",
                })
                continue
            total_bytes += len(data)
            filename = loaded.get("filename") or f"file_{fid}"
            content_type = (
                loaded.get("content_type") or "application/octet-stream"
            )
            rt_attachments.append(
                _RTAttachment(
                    file_name=filename,
                    file_type=content_type,
                    file_content=data,
                )
            )
            meta.append({
                "file_id": loaded.get("file_id") or fid,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": loaded.get("size_bytes"),
            })

        return rt_attachments, meta, skipped


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
