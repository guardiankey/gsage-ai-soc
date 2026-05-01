"""gSage AI — Trellix EDR generic search tool.

Dispatches to the v2 realtime SQL-like search (``query`` param) or to the
v1 Active Response structured search (``payload`` param).  Always runs in
the background — the search starts on Trellix, the worker polls until the
result is ready (HTTP 303), then fetches all pages, summarises and
optionally exports CSV/JSON.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._artifacts import build_agent_payload
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class TrellixEdrSearchTool(BaseTool):
    """Run a Trellix EDR search and return summarised results.

    Pass exactly one of:

    - ``query``   — v2 SQL-like (``"HostInfo hostname WHERE HostInfo hostname contains 'foo'"``).
    - ``payload`` — v1 structured Active Response payload (``{"projections": [...], "condition": {...}}``).

    v2 SYNTAX QUICK REFERENCE
    -------------------------
    Shape::

        <Collector1 f1, f2 [AND Collector2 f3, f4 ...]> [WHERE <cond1> [AND|OR <cond2> ...]]

    Rules:

    1. **Projections are mandatory.** Always list at least one field per
       collector you want columns from. ``ScheduledTasks`` alone is invalid;
       use ``ScheduledTasks folder, taskname``. Fields **inside the same
       collector** are separated by COMMAS.
    2. **Multi-collector / auto-join.** Different collectors are joined
       with the ``AND`` keyword (NOT a comma). Trellix joins them by host
       automatically. Example:
       ``ScheduledTasks folder, taskname AND HostInfo hostname``.

       ``HostInfo`` IS a regular collector and CAN be combined with any
       other collector via ``AND`` — if any prior reasoning claimed
       otherwise it is wrong. The following are official Trellix examples,
       valid as-is::

           HostInfo hostname
               AND Software installdate, publisher, version, displayname
                   WHERE Software displayname contains "zoom"

           HostInfo hostname
               AND InteractiveSessions userid, name
                   WHERE HostInfo hostname equals "PC1"

           HostInfo hostname
               AND Files created_at, last_write, name
                   WHERE Files full_name contains "manifest.json"
    3. **WHERE may reference any collector.** A collector cited only in the
       WHERE clause does not need to appear in the projection list. Example:
       ``ScheduledTasks folder, taskname WHERE HostInfo hostname contains 'PR7009065'``
       is valid even though ``HostInfo`` is not projected.
    4. **Conditions** are ``Collector field <op> <value>`` joined by
       ``AND`` / ``OR``. Common ops: ``equals``, ``not equals``,
       ``contains``, ``starts_with``, ``ends_with``, ``greater_than``,
       ``less_than``, ``before``, ``after``. **String literals use DOUBLE
       QUOTES** (``equals "PR7009065"``). Numeric values — including IPv4
       addresses — are written **unquoted** (``HostInfo ip_address equals
       192.168.0.5``, ``NetworkFlow dst_port equals 445``).
    5. **Discoverability — MANDATORY pre-flight.** Before composing ANY
       v2 query, call ``trellix_edr_collectors`` with the collector name
       to retrieve the exact field list. Trellix sometimes accepts
       hallucinated field names **silently** and returns empty / partial
       rows that look like a success but are not — do NOT guess names
       based on other tools (Sysinternals, WMI, osquery): there is no
       ``pid`` (it is ``id``), no ``displayname`` for Services (it is
       ``description``), no ``cmdline`` for ScheduledTasks (it is
       ``task_run``), etc. Always look up the real schema first. Unknown
       fields fail with ``AR-806`` when not silently swallowed.
    6. **Fallback when a v2 query keeps returning HTTP 400 "Invalid value
       provided for query"**: switch to the explicit cross-collector form
       (project HostInfo too, joined with ``AND``) and re-issue. Example:
       ``ScheduledTasks taskname, folder AND HostInfo hostname WHERE
       HostInfo hostname equals "PR7009065"``. If it still fails after 2
       attempts, switch to the v1 ``payload`` form using the
       ``v1_payload_example`` from ``trellix_edr_collectors`` as a template
       — do not keep retrying the same v2 shape.

    Examples::

        # Single collector with explicit projection (commas between fields)
        Processes name, pid, command_line WHERE Processes name equals "powershell.exe"

        # Filter by host without projecting HostInfo (compact form)
        ScheduledTasks folder, taskname, status WHERE HostInfo hostname equals "PR7009065"

        # Same intent, explicit cross-collector form (use this if the compact
        # form returns HTTP 400)
        ScheduledTasks folder, taskname, status AND HostInfo hostname
            WHERE HostInfo hostname equals "PR7009065"

        # Cross-collector projection — 'AND' between collectors, not comma
        Processes name, pid AND HostInfo hostname WHERE Processes name contains "cmd"

        # Numeric / IP values are unquoted
        Processes id, name WHERE HostInfo ip_address equals 10.9.9.9
        NetworkFlow dst_ip, dst_port WHERE NetworkFlow dst_port equals 445

    Always background, since searches typically take 30 s – 5 min.

    Output (``data``)::

        query_id, api_version, total_count, total_hosts, truncated,
        rows_total, rows_overflow, rows_preview_limit, agent_hint,
        summary: { row_count, distinct, top, sample },
        rows: [...up to 100 inlined...],
        artifacts: { csv_file, json_file }
    """

    name: ClassVar[str] = "trellix_edr_search"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Hunt across endpoints with Trellix EDR realtime searches "
        "(SQL-like v2 or structured v1)"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 900
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    always_background: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.TRELLIX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.TRELLIX_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "query"}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "v2 realtime search expression (SQL-like). Shape: "
                    "'<Collector1 f1, f2 [AND Collector2 f3, f4 ...]> "
                    "[WHERE <Collector field op value> [AND|OR ...]]'. "
                    "RULES: (1) projections are MANDATORY — never write a "
                    "bare collector name like 'ScheduledTasks'; always list "
                    "at least one field. Inside ONE collector fields are "
                    "separated by COMMAS. (2) Different collectors are "
                    "joined with the 'AND' keyword (NOT a comma): "
                    "'Processes name, pid AND HostInfo hostname'. HostInfo "
                    "IS a real collector and CAN be combined with ANY "
                    'other collector via AND \u2014 \'HostInfo hostname AND '
                    'Software displayname, version WHERE Software '
                    'displayname contains "zoom"\' is a valid official '
                    "Trellix example. "
                    "(3) WHERE may reference ANY collector even if not "
                    "projected \u2014 Trellix auto-joins by host. (4) STRING "
                    'literals are wrapped in plain double-quote characters '
                    '("). Write them LITERALLY inside the query value, e.g. '
                    'the query value is exactly: Software displayname WHERE '
                    'Software displayname contains "Trellix"  \u2014 do NOT '
                    'prefix the quotes with a backslash; the backslashes '
                    'shown elsewhere in this description are only JSON '
                    'string-escaping. Numeric values and IPs are UNQUOTED: '
                    "'equals 445', 'equals 10.0.0.1'. "
                    "(5) MANDATORY: before composing "
                    "the query, call trellix_edr_collectors with the "
                    "collector name to get the real field list \u2014 Trellix "
                    "may silently accept hallucinated names and return "
                    "empty rows that look like success. Do NOT guess from "
                    "Sysinternals/WMI/osquery names: Processes uses 'id' "
                    "(not 'pid'), Services uses 'description' (not "
                    "'displayname') and 'startuptype' (not 'starttype'), "
                    "ScheduledTasks uses 'taskname'+'folder' (not "
                    "'name'+'path'), etc. Unknown fields fail with AR-806 "
                    "when not silently swallowed. (6) If a v2 query "
                    "returns HTTP 400 'Invalid value provided for query', "
                    "try the explicit form projecting HostInfo too with "
                    'AND, e.g. ScheduledTasks taskname, folder AND HostInfo '
                    'hostname WHERE HostInfo hostname equals "host01". '
                    "After 2 failed v2 attempts, switch to "
                    "'payload' (v1) using the v1_payload_example from "
                    "trellix_edr_collectors as a template. Mutually "
                    "exclusive with 'payload'."
                ),
            },
            "payload": {
                "type": "object",
                "description": (
                    "v1 Active Response structured payload "
                    "({\"projections\": [...], \"condition\": {...}}).  "
                    "Mutually exclusive with 'query'."
                ),
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
                "description": (
                    f"Maximum rows to include in 'rows' (default {Q.DEFAULT_MAX_ROWS}, "
                    f"hard cap {Q.HARD_MAX_ROWS}).  Full result set is always included "
                    "in the CSV/JSON artifact when export is enabled."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all rows as a CSV file artifact. PREFER CSV "
                    "over JSON for tabular search results — it is smaller, "
                    "easier for the user to open in Excel/spreadsheets and "
                    "the natural format for these flat row sets. When the "
                    "caller asks to 'save the results' / 'export' / "
                    "'download' without specifying a format, set this to "
                    "true and leave 'export_json' false."
                ),
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all rows as a JSON file artifact. Only use "
                    "when the user explicitly asks for JSON or needs the "
                    "file for programmatic post-processing — otherwise "
                    "prefer 'export_csv'."
                ),
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of column names to use for top-N analytics "
                    "(overrides the default heuristic).  Use names from the "
                    "flattened result (e.g. 'HostInfo_hostname', 'Files_sha1')."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Top-N size for each grouped column (default: 10).",
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        query = params.get("query")
        payload = params.get("payload")

        if (query and payload) or (not query and not payload):
            return self._failure(
                "INVALID_INPUT",
                "Provide exactly one of 'query' (v2) or 'payload' (v1).",
            )

        if query and not isinstance(query, str):
            return self._failure("INVALID_INPUT", "'query' must be a string.")
        if payload and not isinstance(payload, dict):
            return self._failure("INVALID_INPUT", "'payload' must be an object.")

        api_version: Q.ApiVersion = "v2" if query else "v1"
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))
        group_by = params.get("group_by") or None
        top_n = int(params.get("top_n", 10) or 10)

        try:
            async with Q.build_client(config) as client:
                query_id, rows, meta, truncated = await Q.run_search_pipeline(
                    client,
                    api_version=api_version,
                    query=query if isinstance(query, str) else None,
                    payload=payload if isinstance(payload, dict) else None,
                    max_rows=max_rows,
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("trellix_edr_search: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        summary = Q.summarize(rows, group_by=group_by, top_n=top_n)
        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=f"trellix_edr_search_{query_id}",
            agent_context=agent_context,
        )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "query_id": query_id,
                "api_version": api_version,
                "total_count": meta.get("total_count", len(rows)),
                "total_hosts": meta.get("total_hosts", 0),
                "truncated": truncated,
                "artifacts": agent_payload["artifacts"],
                "rows_total": agent_payload["rows_total"],
                "rows_overflow": agent_payload["rows_overflow"],
                "agent_hint": agent_payload["agent_hint"],
                "rows_preview_limit": Q.AGENT_PREVIEW_ROWS,
                "summary": summary,
                "rows": agent_payload["rows_preview"],
            },
            execution_time_ms=elapsed,
        )
