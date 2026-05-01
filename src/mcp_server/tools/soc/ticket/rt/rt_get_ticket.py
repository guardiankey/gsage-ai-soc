"""gSage AI — RT Get Ticket tool.

Fetches a single Request Tracker ticket and, optionally, related
sub-resources (history/links/attachments index/requestor profile).
Sub-resource fetches run in parallel via :func:`asyncio.gather`.

Required permission: ``rt:read``
"""

from __future__ import annotations

import asyncio
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

log = logging.getLogger(__name__)

_VALID_INCLUDE = {
    "history",
    "links",
    "attachments",
    "requestors",
    "cc",
    "admin_cc",
    "custom_fields",
}

# Cap for sub-resources to keep the LLM payload bounded.
_MAX_HISTORY_ENTRIES = 50
_MAX_ATTACHMENTS = 50

# Whitelist of "core" ticket fields surfaced to the agent.  Anything else is
# pushed under ``custom_fields`` only when explicitly requested.
_TICKET_CORE_FIELDS = {
    "id",
    "Subject",
    "Status",
    "Queue",
    "Owner",
    "Creator",
    "Created",
    "LastUpdated",
    "Started",
    "Resolved",
    "Due",
    "Priority",
    "InitialPriority",
    "FinalPriority",
    "TimeWorked",
    "TimeEstimated",
    "TimeLeft",
}


def _compact_ticket(raw: dict) -> dict:
    """Reduce a full RT ticket payload to its core fields."""
    out: dict[str, Any] = {}
    for k in _TICKET_CORE_FIELDS:
        v = raw.get(k)
        if v not in (None, "", []):
            out[k.lower() if k != "id" else "id"] = v
    return out


def _compact_history_entry(entry: dict) -> dict:
    """Whitelist a history entry — the heavy ``Content`` field is preserved."""
    return {
        "id": entry.get("id"),
        "type": entry.get("Type"),
        "created": entry.get("Created"),
        "creator": entry.get("Creator"),
        "field": entry.get("Field"),
        "old_value": entry.get("OldValue"),
        "new_value": entry.get("NewValue"),
        "description": entry.get("Description"),
        "content": entry.get("Content"),
        "content_type": entry.get("ContentType"),
    }


def _compact_attachment(att: dict) -> dict:
    return {
        "id": att.get("id"),
        "filename": att.get("Filename") or att.get("filename"),
        "content_type": att.get("ContentType") or att.get("content_type"),
        "size_bytes": att.get("ContentLength") or att.get("size"),
        "subject": att.get("Subject"),
        "creator": att.get("Creator"),
        "created": att.get("Created"),
    }


def _compact_link(link: dict) -> dict:
    return {
        "type": link.get("type") or link.get("Type"),
        "ref": link.get("ref") or link.get("URI"),
        "id": link.get("id"),
    }


def _normalise_actors(raw: Any) -> list[dict]:
    """RT may return Requestor/Cc/AdminCc as a string, list[str] or list[dict]."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [{"id": raw}]
    if isinstance(raw, list):
        out: list[dict] = []
        for item in raw:
            if isinstance(item, str):
                out.append({"id": item})
            elif isinstance(item, dict):
                out.append({
                    "id": item.get("id") or item.get("Name"),
                    "email": item.get("EmailAddress"),
                    "real_name": item.get("RealName"),
                })
        return out
    return []


async def _fetch_history(client: RTClient, ticket_id: int) -> list[dict]:
    rows = await client.get_ticket_history(ticket_id)
    return [_compact_history_entry(r) for r in rows[:_MAX_HISTORY_ENTRIES]]


async def _fetch_attachments(client: RTClient, ticket_id: int) -> list[dict]:
    rows = await client.get_attachments(ticket_id)
    return [_compact_attachment(r) for r in rows[:_MAX_ATTACHMENTS]]


async def _fetch_links(client: RTClient, ticket_id: int) -> list[dict]:
    rows = await client.get_links(ticket_id)
    return [_compact_link(r) for r in rows]


class RTGetTicketTool(BaseTool):
    """Fetch a single RT ticket plus optional related data.

    Pass ``include`` to attach extras: ``history`` (audit trail with
    correspondence/comments), ``links`` (RefersTo/MemberOf/etc),
    ``attachments`` (index only — use ``rt_manage`` action
    ``fetch_attachment`` to download), ``requestors``, ``cc``, ``admin_cc``,
    ``custom_fields``.

    Permission: ``rt:read``.
    """

    name: ClassVar[str] = "rt_get_ticket"
    config_namespace: ClassVar[str] = "rt"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Fetch one RT ticket with optional related data"
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["rt:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 45
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {"target_id": "ticket_id"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["ticket_id"],
        "properties": {
            "ticket_id": {
                "type": "integer",
                "minimum": 1,
                "description": "RT ticket numeric id.",
            },
            "include": {
                "type": "array",
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": sorted(_VALID_INCLUDE),
                },
                "description": (
                    "Extra sub-resources to attach. Each runs in parallel. "
                    "'attachments' returns the index only — use rt_manage "
                    "with action='fetch_attachment' to download a file."
                ),
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
        try:
            ticket_id = int(params["ticket_id"])
        except (KeyError, ValueError, TypeError):
            return self._failure("INVALID_PARAMS", "ticket_id is required and must be a positive integer.")
        if ticket_id <= 0:
            return self._failure("INVALID_PARAMS", "ticket_id must be > 0.")

        include = set(params.get("include") or [])
        unknown = include - _VALID_INCLUDE
        if unknown:
            return self._failure(
                "INVALID_PARAMS",
                f"unknown include value(s): {sorted(unknown)}. "
                f"Allowed: {sorted(_VALID_INCLUDE)}.",
            )

        try:
            async with build_rt_client(config) as client:
                raw_ticket = await client.get_ticket(ticket_id)

                # Schedule sub-resource fetches in parallel.
                jobs: dict[str, Any] = {}
                if "history" in include:
                    jobs["history"] = _fetch_history(client, ticket_id)
                if "links" in include:
                    jobs["links"] = _fetch_links(client, ticket_id)
                if "attachments" in include:
                    jobs["attachments"] = _fetch_attachments(client, ticket_id)

                results: dict[str, Any] = {}
                if jobs:
                    keys = list(jobs.keys())
                    gathered = await asyncio.gather(
                        *jobs.values(), return_exceptions=True
                    )
                    for k, val in zip(keys, gathered):
                        if isinstance(val, Exception):
                            log.warning(
                                "rt_get_ticket: sub-fetch %r failed: %s", k, val
                            )
                            results[k] = {"error": str(val)}
                        else:
                            results[k] = val
        except RTError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("rt_get_ticket: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        ticket = _compact_ticket(raw_ticket)

        if "requestors" in include:
            ticket["requestors"] = _normalise_actors(
                raw_ticket.get("Requestor") or raw_ticket.get("Requestors")
            )
        if "cc" in include:
            ticket["cc"] = _normalise_actors(raw_ticket.get("Cc"))
        if "admin_cc" in include:
            ticket["admin_cc"] = _normalise_actors(raw_ticket.get("AdminCc"))
        if "custom_fields" in include:
            cfs = raw_ticket.get("CustomFields") or {}
            # python-rt returns CFs as a list of {id, name, values} entries on
            # newer RTs; collapse to {name: values}.
            if isinstance(cfs, list):
                ticket["custom_fields"] = {
                    (c.get("name") or c.get("id")): c.get("values")
                    for c in cfs
                    if isinstance(c, dict)
                }
            elif isinstance(cfs, dict):
                ticket["custom_fields"] = cfs

        # Merge sub-resources at the top level for a flat consumer payload.
        for k, v in results.items():
            ticket[k] = v

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"ticket": ticket, "id": ticket_id},
            execution_time_ms=elapsed,
        )
