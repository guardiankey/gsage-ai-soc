"""gSage AI — Zabbix read-only query tool.

Provides a single ``zabbix_query`` MCP tool with 13 actions covering:

- Inventory  : hosts_list, host_details, hostgroups_list, hosts_in_group,
               templates_list, items_list, maintenance_list
- Health      : problems_list, events_list, triggers_list, severity_summary,
               host_health (consolidated)
- Metrics     : metric_history

Authentication: API token (preferred) or username/password.
Multi-instance: one GSageToolConfig row per Zabbix server
(profile_id = "prod", "homol", "client-a", etc.).

Permission: ``zabbix:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.monitoring.zabbix._client import ZabbixClient, ZabbixError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared config schema / defaults
# ---------------------------------------------------------------------------

_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["url"],
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "Zabbix frontend URL (e.g. https://zabbix.example.com). "
                "Must be accessible from the mcp-server container."
            ),
        },
        "token": {
            "type": "string",
            "description": "Zabbix API token (preferred auth method). "
                           "Generated in User settings → API tokens.",
            "sensitive": True,
        },
        "username": {
            "type": "string",
            "description": "Zabbix username (used when 'token' is not set).",
        },
        "password": {
            "type": "string",
            "description": "Zabbix password (used together with 'username').",
            "sensitive": True,
        },
        "verify_tls": {
            "type": "boolean",
            "description": "Verify the server TLS certificate (default: true).",
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "HTTP request timeout in seconds (default: 30).",
        },
        "skip_version_check": {
            "type": "boolean",
            "description": (
                "Skip zabbix-utils API version compatibility check "
                "(default: false). Enable for Zabbix 8.x or other versions "
                "newer than what the library has been tested with."
            ),
        },
    },
    "additionalProperties": False,
}

_CONFIG_DEFAULTS: dict = {
    "verify_tls": True,
    "timeout": 30,
    "skip_version_check": False,
}

# ---------------------------------------------------------------------------
# History value-type codes
# ---------------------------------------------------------------------------
_HISTORY_TYPES = {
    "float": 0,
    "character": 1,
    "log": 2,
    "integer": 3,
    "text": 4,
}

# Severity labels used internally
_SEVERITY_LABELS = {
    0: "not_classified",
    1: "info",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}

# Item keys polled in host_health (silently skipped if missing on the host)
_HEALTH_ITEM_KEYS = [
    "system.cpu.util",
    "vm.memory.size[pavailable]",
    "vfs.fs.size[/,pused]",
    "system.uptime",
    "agent.ping",
]

# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ZabbixQueryTool(BaseTool):
    """Read-only query tool for Zabbix monitoring platform.

    Supports 13 actions grouped into three categories:

    **Inventory**
    - ``hosts_list`` — search hosts by name pattern or IP address.
    - ``host_details`` — full host record (interfaces, groups, templates,
      inventory, macros).
    - ``hostgroups_list`` — list or search host groups.
    - ``hosts_in_group`` — all hosts belonging to a specific group.
    - ``templates_list`` — templates linked to a host (or search by name).
    - ``items_list`` — list items (metrics) available on a host; essential
      for obtaining ``itemid`` values required by ``metric_history``.
    - ``maintenance_list`` — scheduled maintenance windows for a host or group.

    **Health / Events**
    - ``problems_list`` — active (or recent) problems, filterable by host and
      severity.
    - ``events_list`` — trigger events (including resolved), filterable by
      host, time range and severity.
    - ``triggers_list`` — all triggers on a host; use ``only_true=true`` to
      show only currently firing triggers.
    - ``severity_summary`` — problem count aggregated by severity for a host
      (or the whole organisation when ``hostid`` is omitted).
    - ``host_health`` — consolidated health snapshot for one host: interface
      availability, active problem counts, maintenance flag, and last values
      of common metrics (CPU, memory, disk, uptime, agent ping).

    **Metrics**
    - ``metric_history`` — recent raw data points for one or more items
      (up to 1 000 data points, newest first).

    Permission: ``zabbix:read``
    """

    name: ClassVar[str] = "zabbix_query"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Query Zabbix monitoring: host inventory, active problems, metric history, "
        "health snapshots and maintenance windows"
    )
    category: ClassVar[str] = "monitoring"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["zabbix:read"]

    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = _CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = _CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "host"}
    # Disable detailed output logging — responses can be large
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            # ── Action selector ────────────────────────────────────────────
            "action": {
                "type": "string",
                "enum": [
                    # inventory
                    "hosts_list",
                    "host_details",
                    "hostgroups_list",
                    "hosts_in_group",
                    "templates_list",
                    "items_list",
                    "maintenance_list",
                    # health / events
                    "problems_list",
                    "events_list",
                    "triggers_list",
                    "severity_summary",
                    "host_health",
                    # metrics
                    "metric_history",
                ],
                "description": (
                    "Operation to perform. See tool description for details on each action."
                ),
            },
            # ── Host selectors ─────────────────────────────────────────────
            "hostid": {
                "type": "string",
                "description": (
                    "Zabbix internal host ID. Use hosts_list to discover "
                    "hostid values from a name or IP."
                ),
            },
            "hostids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of Zabbix host IDs (use instead of hostid when multiple).",
            },
            "host": {
                "type": "string",
                "description": (
                    "Filter hosts by technical name (partial match). "
                    "Used with hosts_list and templates_list."
                ),
            },
            "host_name": {
                "type": "string",
                "description": (
                    "Filter hosts by visible/display name (partial match). "
                    "Used with hosts_list."
                ),
            },
            "ip": {
                "type": "string",
                "description": "Filter hosts by IP address (exact match). Used with hosts_list.",
            },
            # ── Group selectors ────────────────────────────────────────────
            "groupid": {
                "type": "string",
                "description": "Zabbix host group ID. Used with hosts_in_group.",
            },
            "groupids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of host group IDs. Used with problems_list, events_list.",
            },
            "group_name": {
                "type": "string",
                "description": "Filter host groups by name (partial match). Used with hostgroups_list.",
            },
            # ── Item / history selectors ───────────────────────────────────
            "itemids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of item IDs to retrieve history for. "
                    "Run action=items_list first to discover valid item IDs."
                ),
            },
            "item_key": {
                "type": "string",
                "description": (
                    "Filter items by key_ (partial match). "
                    "Example: 'cpu', 'memory', 'vfs.fs'. Used with items_list."
                ),
            },
            "item_name": {
                "type": "string",
                "description": "Filter items by display name (partial match). Used with items_list.",
            },
            "history_type": {
                "type": "string",
                "enum": ["float", "integer", "character", "log", "text"],
                "description": (
                    "Item value type for metric_history. "
                    "Use 'float' for numeric gauges (CPU, memory), "
                    "'integer' for counters, 'character'/'text'/'log' for string values. "
                    "Default: 'float'."
                ),
            },
            # ── Time range ─────────────────────────────────────────────────
            "time_from": {
                "type": "integer",
                "description": "Start of time range as Unix timestamp.",
            },
            "time_till": {
                "type": "integer",
                "description": "End of time range as Unix timestamp.",
            },
            # ── Severity / problem filters ─────────────────────────────────
            "severities": {
                "type": "array",
                "items": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5,
                },
                "description": (
                    "Severity codes to include: 0=not_classified, 1=info, "
                    "2=warning, 3=average, 4=high, 5=disaster."
                ),
            },
            "min_severity": {
                "type": "integer",
                "minimum": 0,
                "maximum": 5,
                "description": (
                    "Minimum severity to include (inclusive). "
                    "Shorthand for severities=[min_severity..5]."
                ),
            },
            "recent": {
                "type": "boolean",
                "description": (
                    "When true, return only recent problems (still active or "
                    "suppressed). Default: true for problems_list."
                ),
            },
            "only_true": {
                "type": "boolean",
                "description": "Return only currently firing triggers (triggers_list only).",
            },
            # ── Pagination ─────────────────────────────────────────────────
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": (
                    "Maximum number of records to return. "
                    "Default: 100 for list actions; 100 for metric_history. "
                    "Max: 500 for list actions; 1000 for metric_history."
                ),
            },
        },
        "additionalProperties": False,
    }

    # ── Execute entry point ────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action: str = params["action"]

        client_kwargs = {
            "url": config.get("url", ""),
            "token": config.get("token") or None,
            "username": config.get("username") or None,
            "password": config.get("password") or None,
            "verify_tls": bool(config.get("verify_tls", True)),
            "timeout": int(config.get("timeout", 30)),
            "skip_version_check": bool(config.get("skip_version_check", False)),
        }

        try:
            async with ZabbixClient(**client_kwargs) as client:
                result = await self._dispatch(action, client, params)
        except ZabbixError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "ZABBIX_ERROR",
                str(exc),
                retryable=exc.retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("zabbix_query: unexpected error (action=%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(result, execution_time_ms=elapsed)

    # ── Dispatcher ────────────────────────────────────────────────────────

    async def _dispatch(
        self,
        action: str,
        client: ZabbixClient,
        params: dict,
    ) -> dict:
        handlers = {
            # inventory
            "hosts_list": self._hosts_list,
            "host_details": self._host_details,
            "hostgroups_list": self._hostgroups_list,
            "hosts_in_group": self._hosts_in_group,
            "templates_list": self._templates_list,
            "items_list": self._items_list,
            "maintenance_list": self._maintenance_list,
            # health / events
            "problems_list": self._problems_list,
            "events_list": self._events_list,
            "triggers_list": self._triggers_list,
            "severity_summary": self._severity_summary,
            "host_health": self._host_health,
            # metrics
            "metric_history": self._metric_history,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ZabbixError(f"Unknown action: {action!r}", retryable=False)
        return await handler(client, params)

    # ── Helper: compute severity filter ───────────────────────────────────

    @staticmethod
    def _severity_filter(params: dict) -> Optional[list[int]]:
        if "severities" in params:
            return [int(s) for s in params["severities"]]
        if "min_severity" in params:
            ms = int(params["min_severity"])
            return list(range(ms, 6))
        return None

    @staticmethod
    def _list_limit(params: dict, max_val: int = 500, default: int = 100) -> int:
        raw = params.get("limit", default)
        return min(int(raw), max_val)

    # =========================================================================
    # Inventory handlers
    # =========================================================================

    async def _hosts_list(self, client: ZabbixClient, params: dict) -> dict:
        rpc: dict = {
            "output": ["hostid", "host", "name", "status", "available",
                       "description", "maintenance_status", "maintenance_type",
                       "maintenanceid"],
            "selectGroups": ["groupid", "name"],
            "selectInterfaces": ["interfaceid", "type", "main", "ip", "dns",
                                 "port", "available"],
            "sortfield": "host",
            "limit": self._list_limit(params),
        }

        # Name / IP search filters
        search: dict = {}
        if "host" in params:
            search["host"] = params["host"]
        if "host_name" in params:
            search["name"] = params["host_name"]
        if search:
            rpc["search"] = search
            rpc["searchByAny"] = True

        # IP filter is applied post-query (Zabbix does not support top-level IP search)
        raw: list = await client.call("host.get", rpc)

        if "ip" in params:
            wanted = params["ip"]
            raw = [
                h for h in raw
                if any(
                    iface.get("ip") == wanted
                    for iface in h.get("interfaces", [])
                )
            ]

        return {"count": len(raw), "hosts": raw}

    async def _host_details(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        host_name = params.get("host")
        if not hostid and not host_name:
            raise ZabbixError(
                "host_details requires 'hostid' or 'host'.", retryable=False
            )

        rpc: dict = {
            "output": "extend",
            "selectGroups": ["groupid", "name"],
            "selectParentTemplates": ["templateid", "host", "name"],
            "selectInterfaces": "extend",
            "selectInventory": "extend",
            "selectMacros": ["macro", "description"],  # exclude values for security
            "selectTags": "extend",
        }
        if hostid:
            rpc["hostids"] = [hostid]
        else:
            rpc["search"] = {"host": host_name}
            rpc["limit"] = 1

        raw: list = await client.call("host.get", rpc)
        if not raw:
            return {"found": False, "host": None}
        return {"found": True, "host": raw[0]}

    async def _hostgroups_list(self, client: ZabbixClient, params: dict) -> dict:
        rpc: dict = {
            "output": ["groupid", "name"],
            "sortfield": "name",
            "limit": self._list_limit(params),
        }
        if "group_name" in params:
            rpc["search"] = {"name": params["group_name"]}
        # Optionally include host IDs in each group
        rpc["selectHosts"] = ["hostid", "host", "name"]

        raw: list = await client.call("hostgroup.get", rpc)
        return {"count": len(raw), "groups": raw}

    async def _hosts_in_group(self, client: ZabbixClient, params: dict) -> dict:
        groupid = params.get("groupid")
        group_name = params.get("group_name")
        if not groupid and not group_name:
            raise ZabbixError(
                "hosts_in_group requires 'groupid' or 'group_name'.", retryable=False
            )

        # Resolve group name to ID if needed
        if not groupid and group_name:
            groups: list = await client.call(
                "hostgroup.get",
                {"output": ["groupid", "name"], "search": {"name": group_name}, "limit": 1},
            )
            if not groups:
                return {"groupid": None, "group_name": group_name, "count": 0, "hosts": []}
            groupid = groups[0]["groupid"]
            group_name = groups[0]["name"]

        rpc: dict = {
            "output": ["hostid", "host", "name", "status", "available"],
            "groupids": [groupid],
            "selectInterfaces": ["ip", "dns", "type", "main"],
            "sortfield": "host",
            "limit": self._list_limit(params),
        }
        raw: list = await client.call("host.get", rpc)
        return {"groupid": groupid, "group_name": group_name, "count": len(raw), "hosts": raw}

    async def _templates_list(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        host_name = params.get("host")

        if hostid or host_name:
            # Get templates linked to a specific host
            host_rpc: dict = {
                "output": ["hostid", "host", "name"],
                "selectParentTemplates": ["templateid", "host", "name"],
            }
            if hostid:
                host_rpc["hostids"] = [hostid]
            else:
                host_rpc["search"] = {"host": host_name}
                host_rpc["limit"] = 1
            hosts: list = await client.call("host.get", host_rpc)
            if not hosts:
                return {"hostid": hostid, "host": host_name, "templates": []}
            h = hosts[0]
            return {
                "hostid": h["hostid"],
                "host": h.get("host"),
                "templates": h.get("parentTemplates", []),
            }

        # Search templates by name
        rpc: dict = {
            "output": ["templateid", "host", "name", "description"],
            "sortfield": "host",
            "limit": self._list_limit(params),
        }
        if "host" in params:
            rpc["search"] = {"host": params["host"]}
        raw: list = await client.call("template.get", rpc)
        return {"count": len(raw), "templates": raw}

    async def _items_list(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        if not hostid:
            raise ZabbixError("items_list requires 'hostid'.", retryable=False)

        rpc: dict = {
            "output": ["itemid", "name", "key_", "value_type", "units",
                       "lastvalue", "lastclock", "state", "error", "status"],
            "hostids": [hostid],
            "sortfield": "key_",
            "limit": self._list_limit(params),
        }
        search: dict = {}
        if "item_key" in params:
            search["key_"] = params["item_key"]
        if "item_name" in params:
            search["name"] = params["item_name"]
        if search:
            rpc["search"] = search
            rpc["searchByAny"] = True

        raw: list = await client.call("item.get", rpc)
        return {"hostid": hostid, "count": len(raw), "items": raw}

    async def _maintenance_list(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        hostids = params.get("hostids", [hostid] if hostid else [])
        groupids = params.get("groupids", [])

        rpc: dict = {
            "output": ["maintenanceid", "name", "description", "maintenance_type",
                       "active_since", "active_till"],
            "selectTimeperiods": ["timeperiodid", "timeperiod_type", "every",
                                  "start_time", "period"],
            "selectHosts": ["hostid", "host", "name"],
            "selectGroups": ["groupid", "name"],
            "sortfield": "name",
            "limit": self._list_limit(params),
        }
        if hostids:
            rpc["hostids"] = [str(h) for h in hostids]
        if groupids:
            rpc["groupids"] = [str(g) for g in groupids]

        raw: list = await client.call("maintenance.get", rpc)
        return {"count": len(raw), "maintenances": raw}

    # =========================================================================
    # Health / Events handlers
    # =========================================================================

    async def _problems_list(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        hostids = params.get("hostids", [hostid] if hostid else [])
        groupids = params.get("groupids", [])

        rpc: dict = {
            "output": ["eventid", "objectid", "name", "severity", "clock",
                       "r_eventid", "acknowledged"],
            "recent": params.get("recent", True),
            # Zabbix 7.x only accepts "eventid" as sortfield for problem.get;
            # Zabbix 6.x also accepts "severity".  Use "eventid" (most recent
            # first) to remain compatible across versions.
            "sortfield": "eventid",
            "sortorder": "DESC",
            "limit": self._list_limit(params),
        }
        if hostids:
            rpc["hostids"] = [str(h) for h in hostids]
        if groupids:
            rpc["groupids"] = [str(g) for g in groupids]

        severities = self._severity_filter(params)
        if severities is not None:
            rpc["severities"] = severities
        if "time_from" in params:
            rpc["time_from"] = params["time_from"]
        if "time_till" in params:
            rpc["time_till"] = params["time_till"]

        raw: list = await client.call("problem.get", rpc)

        # Enrich each problem with its associated hosts.
        # problem.get does not expose hosts directly; objectid = triggerid,
        # so a single trigger.get call resolves all hosts in one round-trip.
        trigger_ids = list({p["objectid"] for p in raw if p.get("objectid")})
        host_by_trigger: dict[str, list[dict]] = {}
        if trigger_ids:
            triggers = await client.call(
                "trigger.get",
                {
                    "output": ["triggerid"],
                    "triggerids": trigger_ids,
                    "selectHosts": ["hostid", "name", "host"],
                },
            )
            host_by_trigger = {t["triggerid"]: t.get("hosts", []) for t in triggers}

        # Annotate severity labels and hosts for LLM readability
        for p in raw:
            p["severity_label"] = _SEVERITY_LABELS.get(int(p.get("severity", 0)), "unknown")
            p["hosts"] = host_by_trigger.get(p.get("objectid", ""), [])

        return {"count": len(raw), "problems": raw}

    async def _events_list(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        hostids = params.get("hostids", [hostid] if hostid else [])
        groupids = params.get("groupids", [])

        rpc: dict = {
            # "r_clock" is NOT a valid output field for event.get; recovery time
            # is not exposed directly — use r_eventid join if needed.
            "output": ["eventid", "objectid", "name", "severity", "clock",
                       "value", "acknowledged", "r_eventid"],
            "source": 0,  # trigger events
            "object": 0,  # trigger
            "sortfield": "clock",
            "sortorder": "DESC",
            "limit": self._list_limit(params),
        }
        if hostids:
            rpc["hostids"] = [str(h) for h in hostids]
        if groupids:
            rpc["groupids"] = [str(g) for g in groupids]

        severities = self._severity_filter(params)
        if severities is not None:
            rpc["severities"] = severities
        if "time_from" in params:
            rpc["time_from"] = params["time_from"]
        if "time_till" in params:
            rpc["time_till"] = params["time_till"]

        raw: list = await client.call("event.get", rpc)

        for e in raw:
            e["severity_label"] = _SEVERITY_LABELS.get(int(e.get("severity", 0)), "unknown")
            e["state"] = "problem" if e.get("value") == "1" else "resolved"

        return {"count": len(raw), "events": raw}

    async def _triggers_list(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        if not hostid:
            raise ZabbixError("triggers_list requires 'hostid'.", retryable=False)

        rpc: dict = {
            "output": ["triggerid", "description", "priority", "value",
                       "lastchange", "status", "error", "comments"],
            "hostids": [hostid],
            "selectItems": ["itemid", "name", "key_"],
            "skipDependent": True,
            "sortfield": "priority",
            "sortorder": "DESC",
            "limit": self._list_limit(params),
        }
        if params.get("only_true"):
            rpc["only_true"] = True

        severities = self._severity_filter(params)
        if severities is not None:
            rpc["min_severity"] = min(severities)

        raw: list = await client.call("trigger.get", rpc)

        for t in raw:
            t["priority_label"] = _SEVERITY_LABELS.get(int(t.get("priority", 0)), "unknown")
            t["state"] = "problem" if t.get("value") == "1" else "ok"

        return {"hostid": hostid, "count": len(raw), "triggers": raw}

    async def _severity_summary(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        hostids = params.get("hostids", [hostid] if hostid else [])
        groupids = params.get("groupids", [])

        rpc: dict = {
            "output": ["eventid", "severity"],
            "recent": True,
            "limit": 1000,
        }
        if hostids:
            rpc["hostids"] = [str(h) for h in hostids]
        if groupids:
            rpc["groupids"] = [str(g) for g in groupids]

        severities = self._severity_filter(params)
        if severities is not None:
            rpc["severities"] = severities

        raw: list = await client.call("problem.get", rpc)

        summary: dict = {label: 0 for label in _SEVERITY_LABELS.values()}
        for p in raw:
            label = _SEVERITY_LABELS.get(int(p.get("severity", 0)), "unknown")
            summary[label] = summary.get(label, 0) + 1

        return {
            "total_problems": len(raw),
            "severity_breakdown": summary,
            "scope": {
                "hostids": [str(h) for h in hostids] if hostids else None,
                "groupids": [str(g) for g in groupids] if groupids else None,
            },
        }

    async def _host_health(self, client: ZabbixClient, params: dict) -> dict:
        hostid = params.get("hostid")
        host_name = params.get("host")
        if not hostid and not host_name:
            raise ZabbixError(
                "host_health requires 'hostid' or 'host'.", retryable=False
            )

        # 1. Resolve host if only name given
        if not hostid:
            hosts: list = await client.call(
                "host.get",
                {
                    "output": ["hostid", "host", "name", "maintenance_status"],
                    "search": {"host": host_name},
                    "limit": 1,
                },
            )
            if not hosts:
                return {"found": False, "host": host_name, "hostid": None}
            h = hosts[0]
            hostid = h["hostid"]
            in_maintenance = h.get("maintenance_status") == "1"
        else:
            hosts_raw: list = await client.call(
                "host.get",
                {
                    "output": ["hostid", "host", "name", "maintenance_status"],
                    "hostids": [hostid],
                },
            )
            if not hosts_raw:
                return {"found": False, "host": None, "hostid": hostid}
            h = hosts_raw[0]
            in_maintenance = h.get("maintenance_status") == "1"

        # 2. Interface availability
        ifaces: list = await client.call(
            "hostinterface.get",
            {
                "output": ["type", "main", "ip", "dns", "available"],
                "hostids": [hostid],
            },
        )
        # Map type codes: 1=agent 2=snmp 3=ipmi 4=jmx
        _iface_type = {1: "agent", 2: "snmp", 3: "ipmi", 4: "jmx"}
        _avail = {"0": "unknown", "1": "available", "2": "unavailable"}
        interfaces_summary = [
            {
                "type": _iface_type.get(int(i.get("type", 0)), "unknown"),
                "main": i.get("main") == "1",
                "ip": i.get("ip"),
                "dns": i.get("dns"),
                "available": _avail.get(str(i.get("available", "0")), "unknown"),
            }
            for i in ifaces
        ]

        # 3. Active problem count by severity
        problems: list = await client.call(
            "problem.get",
            {
                "output": ["eventid", "severity"],
                "hostids": [hostid],
                "recent": True,
                "limit": 500,
            },
        )
        severity_counts: dict = {label: 0 for label in _SEVERITY_LABELS.values()}
        for p in problems:
            label = _SEVERITY_LABELS.get(int(p.get("severity", 0)), "unknown")
            severity_counts[label] = severity_counts.get(label, 0) + 1

        # 4. Last values for common health items (silently skip missing keys)
        all_items: list = await client.call(
            "item.get",
            {
                "output": ["itemid", "key_", "name", "lastvalue", "lastclock",
                           "units", "value_type"],
                "hostids": [hostid],
                "search": {"key_": "system"},
                "searchByAny": True,
                "filter": {"status": "0"},  # only enabled items
            },
        )
        # Merge with agent.ping and memory/fs items
        extra_items: list = await client.call(
            "item.get",
            {
                "output": ["itemid", "key_", "name", "lastvalue", "lastclock",
                           "units", "value_type"],
                "hostids": [hostid],
                "search": {"key_": "agent.ping"},
                "filter": {"status": "0"},
            },
        )
        extra_mem: list = await client.call(
            "item.get",
            {
                "output": ["itemid", "key_", "name", "lastvalue", "lastclock",
                           "units", "value_type"],
                "hostids": [hostid],
                "search": {"key_": "vm.memory"},
                "filter": {"status": "0"},
            },
        )
        extra_fs: list = await client.call(
            "item.get",
            {
                "output": ["itemid", "key_", "name", "lastvalue", "lastclock",
                           "units", "value_type"],
                "hostids": [hostid],
                "search": {"key_": "vfs.fs"},
                "filter": {"status": "0"},
            },
        )

        merged: dict = {}
        for item in all_items + extra_items + extra_mem + extra_fs:
            k = item.get("key_", "")
            for health_key in _HEALTH_ITEM_KEYS:
                # Prefix match so "system.cpu.util[,guest]" → "system.cpu.util"
                if k.startswith(health_key.split("[")[0]) and health_key not in merged:
                    merged[health_key] = {
                        "itemid": item.get("itemid"),
                        "key_": k,
                        "name": item.get("name"),
                        "lastvalue": item.get("lastvalue"),
                        "lastclock": item.get("lastclock"),
                        "units": item.get("units"),
                    }
                    break

        return {
            "found": True,
            "hostid": hostid,
            "host": h.get("host"),
            "name": h.get("name"),
            "in_maintenance": in_maintenance,
            "interfaces": interfaces_summary,
            "active_problems": {
                "total": len(problems),
                "by_severity": severity_counts,
            },
            "key_metrics": merged,
        }

    # =========================================================================
    # Metrics handler
    # =========================================================================

    async def _metric_history(self, client: ZabbixClient, params: dict) -> dict:
        itemids = params.get("itemids", [])
        if not itemids:
            raise ZabbixError(
                "metric_history requires 'itemids'. "
                "Run action=items_list first to discover valid item IDs.",
                retryable=False,
            )

        history_type_str = params.get("history_type", "float")
        history_code = _HISTORY_TYPES.get(history_type_str, 0)
        limit = min(int(params.get("limit", 100)), 1000)

        rpc: dict = {
            "output": "extend",
            "history": history_code,
            "itemids": [str(i) for i in itemids],
            "sortfield": "clock",
            "sortorder": "DESC",
            "limit": limit,
        }
        if "time_from" in params:
            rpc["time_from"] = params["time_from"]
        if "time_till" in params:
            rpc["time_till"] = params["time_till"]

        raw: list = await client.call("history.get", rpc)

        return {
            "item_count": len(set(r.get("itemid") for r in raw)),
            "record_count": len(raw),
            "history_type": history_type_str,
            "note": (
                "Zabbix history retention is finite. For long-term aggregated data "
                "use action=metric_history over shorter windows or query Zabbix trends "
                "(trend.get) directly."
            ),
            "data": raw,
        }
