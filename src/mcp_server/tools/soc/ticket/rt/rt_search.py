"""gSage AI — RT Search tool.

Searches Request Tracker (RT) tickets or queues using either TicketSQL
(raw query) or a structured ``filters`` object the LLM can populate
without knowing the TicketSQL grammar.

Required permission: ``rt:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.rt._client import (
    RT_CONFIG_DEFAULTS,
    RT_CONFIG_SCHEMA,
    RTError,
    build_rt_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_MAX_LIMIT = 50
_MAX_RAW_QUERY_LEN = 1024


def _quote(value: str) -> str:
    """Quote a TicketSQL string literal, escaping single quotes."""
    return "'" + str(value).replace("'", "\\'") + "'"


def _compose_query(filters: dict) -> str:
    """Build a TicketSQL fragment from a structured ``filters`` dict.

    Recognised keys: ``queue``, ``status``, ``owner``, ``requester``,
    ``subject_contains``, ``content_contains``, ``created_after``,
    ``created_before``, ``updated_after``, ``updated_before``,
    ``id``, ``ids``.  Unknown keys are ignored.
    """
    clauses: list[str] = []

    def _multi_or(field: str, values: list[str]) -> Optional[str]:
        clean = [v for v in values if v]
        if not clean:
            return None
        if len(clean) == 1:
            return f"{field} = {_quote(clean[0])}"
        return "(" + " OR ".join(f"{field} = {_quote(v)}" for v in clean) + ")"

    queue = filters.get("queue")
    if isinstance(queue, str) and queue:
        clauses.append(f"Queue = {_quote(queue)}")
    elif isinstance(queue, list):
        c = _multi_or("Queue", queue)
        if c:
            clauses.append(c)

    status = filters.get("status")
    if isinstance(status, str) and status:
        if status == "active":
            clauses.append("(Status='new' OR Status='open' OR Status='stalled')")
        else:
            clauses.append(f"Status = {_quote(status)}")
    elif isinstance(status, list):
        c = _multi_or("Status", status)
        if c:
            clauses.append(c)

    owner = filters.get("owner")
    if isinstance(owner, str) and owner:
        clauses.append(f"Owner = {_quote(owner)}")

    requester = filters.get("requester")
    if isinstance(requester, str) and requester:
        clauses.append(f"Requestor.EmailAddress LIKE {_quote(requester)}")

    subject = filters.get("subject_contains")
    if isinstance(subject, str) and subject:
        clauses.append(f"Subject LIKE {_quote('%' + subject + '%')}")

    content = filters.get("content_contains")
    if isinstance(content, str) and content:
        clauses.append(f"Content LIKE {_quote('%' + content + '%')}")

    for key, op in (
        ("created_after", "Created >"),
        ("created_before", "Created <"),
        ("updated_after", "LastUpdated >"),
        ("updated_before", "LastUpdated <"),
    ):
        v = filters.get(key)
        if isinstance(v, str) and v:
            clauses.append(f"{op} {_quote(v)}")

    ids = filters.get("ids")
    if isinstance(ids, list) and ids:
        valid = [str(int(i)) for i in ids if isinstance(i, (int, str)) and str(i).isdigit()]
        if valid:
            clauses.append("(" + " OR ".join(f"id = {i}" for i in valid) + ")")
    elif isinstance(filters.get("id"), int):
        clauses.append(f"id = {int(filters['id'])}")

    return " AND ".join(clauses)


def _compact_ticket_row(row: dict) -> dict:
    """Reduce a raw RT search row to the fields the agent actually needs."""
    out = {
        "id": row.get("id"),
        "subject": row.get("Subject"),
        "status": row.get("Status"),
        "queue": row.get("Queue"),
        "owner": row.get("Owner"),
        "requestor": row.get("Requestor") or row.get("Requestors"),
        "created": row.get("Created"),
        "last_updated": row.get("LastUpdated"),
        "due": row.get("Due"),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _compact_queue_row(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("Name") or row.get("name"),
        "description": row.get("Description"),
        "disabled": row.get("Disabled"),
        "lifecycle": row.get("Lifecycle"),
    }


class RTSearchTool(BaseTool):
    """Search RT tickets or list RT queues.

    Use ``filters`` for the common ticket cases — the tool builds the
    TicketSQL for you.  Use ``query`` only when you need an expression
    that ``filters`` cannot express (advanced TicketSQL grammar).

    **Examples:**

    - "open tickets in queue Operações" →
      ``entity="ticket"``, ``filters={"queue": "Operações", "status": "active"}``
    - "tickets owned by maria created in the last 7 days" →
      ``entity="ticket"``, ``filters={"owner": "maria", "created_after": "2026-04-24"}``
    - "list active queues" → ``entity="queue"``

    To look up a single user, group or attachment, use ``rt_get_ticket``
    or ``rt_manage`` instead — those are the canonical entry points for
    those entities.

    Permission: ``rt:read``.
    """

    name: ClassVar[str] = "rt_search"
    config_namespace: ClassVar[str] = "rt"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search RT tickets via structured filters or TicketSQL; list RT queues"
    )
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["rt:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": [],
        "properties": {
            "entity": {
                "type": "string",
                "enum": ["ticket", "queue"],
                "default": "ticket",
                "description": "Entity type to search.",
            },
            "query": {
                "type": "string",
                "maxLength": _MAX_RAW_QUERY_LEN,
                "description": (
                    "Raw TicketSQL expression (tickets only). When "
                    "provided, takes precedence over ``filters``. "
                    "Max 1024 chars."
                ),
            },
            "filters": {
                "type": "object",
                "description": (
                    "Structured filters (tickets only). Composed into "
                    "TicketSQL. Keys: queue (str|list), status "
                    "(str|list, or 'active'), owner, requester, "
                    "subject_contains, content_contains, created_after, "
                    "created_before, updated_after, updated_before, id, ids."
                ),
                "properties": {
                    "queue": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                    "status": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                    "owner": {"type": "string"},
                    "requester": {"type": "string"},
                    "subject_contains": {"type": "string"},
                    "content_contains": {"type": "string"},
                    "created_after": {"type": "string"},
                    "created_before": {"type": "string"},
                    "updated_after": {"type": "string"},
                    "updated_before": {"type": "string"},
                    "id": {"type": "integer", "minimum": 1},
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "maxItems": 50,
                    },
                },
                "additionalProperties": False,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_LIMIT,
                "default": 20,
                "description": f"Max rows to return (hard cap {_MAX_LIMIT}).",
            },
            "order": {
                "type": "string",
                "description": (
                    "RT order parameter, e.g. ``-Created`` (newest first). "
                    "Tickets only."
                ),
            },
            "include_disabled": {
                "type": "boolean",
                "default": False,
                "description": "When entity='queue', include disabled queues.",
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
        entity = params.get("entity", "ticket")
        limit = min(int(params.get("limit") or 20), _MAX_LIMIT)
        raw_query = (params.get("query") or "").strip()
        filters = params.get("filters") or {}

        if raw_query and len(raw_query) > _MAX_RAW_QUERY_LEN:
            return self._failure(
                "INVALID_PARAMS",
                f"query exceeds maximum length of {_MAX_RAW_QUERY_LEN} chars.",
            )

        query = raw_query or _compose_query(filters)
        if entity == "ticket" and not query:
            return self._failure(
                "INVALID_PARAMS",
                "Provide either 'query' (TicketSQL) or non-empty 'filters'.",
            )

        try:
            async with build_rt_client(config) as client:
                if entity == "ticket":
                    rows = await client.search_tickets(
                        query=query,
                        order=params.get("order") or None,
                        per_page=limit,
                    )
                    compact_rows = [_compact_ticket_row(r) for r in rows]
                else:  # queue
                    raw_rows = await client.get_all_queues(
                        include_disabled=bool(params.get("include_disabled", False))
                    )
                    rows = raw_rows[:limit]
                    compact_rows = [_compact_queue_row(r) for r in rows]
        except RTError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("rt_search: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        truncated = len(rows) >= limit

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "entity": entity,
                "count": len(compact_rows),
                "truncated": truncated,
                "query": query or None,
                "rows": compact_rows,
            },
            execution_time_ms=elapsed,
        )
