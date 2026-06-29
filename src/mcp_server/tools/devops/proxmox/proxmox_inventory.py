"""gSage AI — Proxmox VE read-only inventory tool.

Exposes an ``action`` enum dispatcher over the read-only Proxmox VE
queries SOC / ops analysts need when triaging or planning a change.
Covers both QEMU/KVM VMs and LXC containers (selected via ``kind`` where
relevant; ``list_vms`` returns both by default).

- ``list_nodes``, ``get_node``, ``cluster_status``
- ``list_vms`` (filters: kind / node / status / templates_only),
  ``get_vm`` (merged config + live status: cores, memory, disks, NICs,
  cloud-init, agent), ``list_templates``
- ``list_storage``, ``list_networks``, ``list_pools``
- ``list_snapshots`` (by vmid or name)
- ``recent_tasks`` — Proxmox cluster task log (audit trail)
- ``get_vm_metrics`` (cpu / memory / disk I/O / net I/O)

A guest can be addressed either by ``vmid`` (unambiguous) or ``name``
(must be unique cluster-wide). Tabular results auto-export as CSV over
100 rows.

Permission: ``proxmox:read``. Multiple clusters via per-profile config.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.proxmox._cache import (
    CACHE_TTL_SECONDS,
    build_cache_key,
    cache_get,
    cache_set,
)
from src.mcp_server.tools.devops.proxmox._client import (
    PROXMOX_CONFIG_DEFAULTS,
    PROXMOX_CONFIG_SCHEMA,
    ProxmoxClient,
    ProxmoxError,
    build_proxmox_client,
)
from src.mcp_server.tools.devops.proxmox import _views as V
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "list_nodes",
    "get_node",
    "cluster_status",
    "list_vms",
    "get_vm",
    "list_templates",
    "list_storage",
    "list_networks",
    "list_pools",
    "list_snapshots",
    "recent_tasks",
    "get_vm_metrics",
})

_KINDS = ("qemu", "lxc")
_DEFAULT_RESULTS = 100
_MAX_RESULTS = 2000


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if val in (None, ""):
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


async def _resolve_guest(
    client: ProxmoxClient, params: dict
) -> tuple[str, str, int]:
    """Resolve ``(node, kind, vmid)`` from ``vmid`` or ``name`` params."""
    vmid = params.get("vmid")
    if vmid not in (None, ""):
        node, kind, _ = await client.locate_guest(int(vmid))
        return node, kind, int(vmid)
    name = (params.get("name") or "").strip()
    if name:
        node, kind, row = await client.find_guest_by_name(name)
        vmid_val = row.get("vmid")
        if vmid_val is None:
            raise _ParamError(
                f"Guest {name!r} found but VMID is missing in the response."
            )
        return node, kind, int(vmid_val)
    raise _ParamError("This action requires 'vmid' or 'name'.")


class ProxmoxInventoryTool(BaseTool):
    """Read-only Proxmox VE inventory (QEMU VMs + LXC containers).

    Use one ``action`` per call. Tabular results auto-export as CSV when
    over 100 rows; the agent receives only the first 100 rows plus a
    download link.

    Permission: ``proxmox:read``.
    """

    name: ClassVar[str] = "proxmox_inventory"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only Proxmox VE inventory: nodes, cluster status, VMs+LXC "
        "with config, templates, storage, networks, pools, snapshots, "
        "tasks, perf metrics. Auto-CSV on >100 rows."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "proxmox"
    permissions: ClassVar[list[str]] = ["proxmox:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "name",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which read-only inventory query to run.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "Name of the configured Proxmox cluster to query (a key "
                    "under the config 'profiles' map). Omit (or 'default') "
                    "for the primary cluster."
                ),
            },
            "vmid": {
                "type": "integer",
                "description": (
                    "Target guest VMID (unambiguous). Required by get_vm / "
                    "list_snapshots / get_vm_metrics unless 'name' is given."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Target guest name (must be unique cluster-wide). "
                    "Alternative to 'vmid'."
                ),
            },
            "node": {
                "type": "string",
                "description": (
                    "Node name. Required by get_node; optional filter for "
                    "list_vms / list_storage / list_networks / recent_tasks."
                ),
            },
            "kind": {
                "type": "string",
                "enum": list(_KINDS),
                "description": (
                    "[list_vms] Filter by guest kind: 'qemu' (KVM VMs) or "
                    "'lxc' (containers). Omit for both."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["running", "stopped"],
                "description": "[list_vms] Filter by power status.",
            },
            "templates_only": {
                "type": "boolean",
                "description": (
                    "[list_vms] Return only templates (default false; "
                    "list_vms excludes templates by default)."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "description": (
                    f"Maximum items to return (default {_DEFAULT_RESULTS}, "
                    f"hard cap {_MAX_RESULTS})."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "description": (
                    "Bypass the Redis cache for this call (still writes the "
                    f"fresh value back). Cache TTL is {CACHE_TTL_SECONDS}s."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "description": (
                    "Force CSV artifact even for small results. CSV is "
                    f"generated automatically over {AGENT_PREVIEW_ROWS} rows."
                ),
            },
            "export_json": {
                "type": "boolean",
                "description": (
                    "Persist the full result as a JSON artifact for "
                    "programmatic post-processing."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = PROXMOX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = PROXMOX_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ─────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = (params.get("action") or "").strip()
        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )
        max_results = min(
            int(params.get("max_results") or _DEFAULT_RESULTS), _MAX_RESULTS
        )

        try:
            async with build_proxmox_client(
                config, profile=params.get("profile")
            ) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(
                    client, params, agent_context, max_results
                )
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except ProxmoxError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED")
            return self._failure(
                exc.code, str(exc), retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("proxmox_inventory(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Tabular + cache helpers ──────────────────────────────────────────────

    async def _tabular(
        self,
        agent_context: AgentContext,
        action: str,
        rows: list[dict],
        params: dict,
        *,
        cache_hit: bool = False,
    ) -> dict:
        agent_payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_{action}",
            agent_context=agent_context,
        )
        return {
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "rows_preview_limit": AGENT_PREVIEW_ROWS,
            "artifacts": agent_payload["artifacts"],
            "agent_hint": agent_payload["agent_hint"],
            "rows": agent_payload["rows_preview"],
            "cache_hit": cache_hit,
        }

    async def _list(
        self,
        agent_context: AgentContext,
        client: ProxmoxClient,
        action: str,
        kind: str,
        params: dict,
        filters: dict,
        rows_fn: Any,
        max_results: int,
    ) -> dict:
        org = str(getattr(agent_context, "org_id", "") or "")
        user = str(getattr(agent_context, "user_id", "") or "")
        profile = str(params.get("profile") or "default")
        key = build_cache_key(
            org_id=org, user_id=user, profile_id=profile,
            pve_host=client.host, kind=kind, filters=filters,
        )
        if not params.get("force_refresh"):
            cached = await cache_get(key)
            if isinstance(cached, list):
                return await self._tabular(
                    agent_context, action, cached[:max_results], params,
                    cache_hit=True,
                )
        rows = await rows_fn()
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, action, rows[:max_results], params
        )

    # ── List actions ─────────────────────────────────────────────────────────

    async def _do_list_nodes(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            return [V.slim_node(n) for n in (await client.get("/nodes") or [])]
        return await self._list(
            agent_context, client, "list_nodes", "nodes",
            params, {}, _rows, max_results,
        )

    async def _do_cluster_status(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        rows = await client.get("/cluster/status") or []
        return {"status": rows}

    async def _do_list_vms(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        kind = (params.get("kind") or "").strip()
        node = (params.get("node") or "").strip()
        status = (params.get("status") or "").strip()
        templates_only = bool(params.get("templates_only"))
        filters = {
            "kind": kind, "node": node, "status": status,
            "templates_only": templates_only,
        }

        async def _rows() -> list[dict]:
            raw = await client.cluster_resources("vm")
            rows = [V.slim_guest_resource(r) for r in raw]
            if templates_only:
                rows = [r for r in rows if r.get("is_template")]
            else:
                rows = [r for r in rows if not r.get("is_template")]
            if kind:
                rows = [r for r in rows if r.get("kind") == kind]
            if node:
                rows = [r for r in rows if r.get("node") == node]
            if status:
                rows = [r for r in rows if r.get("status") == status]
            return rows
        return await self._list(
            agent_context, client, "list_vms", "vms",
            params, filters, _rows, max_results,
        )

    async def _do_list_templates(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            raw = await client.cluster_resources("vm")
            rows = [V.slim_guest_resource(r) for r in raw]
            return [r for r in rows if r.get("is_template")]
        return await self._list(
            agent_context, client, "list_templates", "templates",
            params, {}, _rows, max_results,
        )

    async def _do_list_storage(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        node = client.resolve_node(params)

        async def _rows() -> list[dict]:
            raw = await client.get(f"/nodes/{node}/storage") or []
            return [V.slim_storage(s) for s in raw]
        return await self._list(
            agent_context, client, "list_storage", "storage",
            params, {"node": node}, _rows, max_results,
        )

    async def _do_list_networks(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        node = client.resolve_node(params)

        async def _rows() -> list[dict]:
            raw = await client.get(f"/nodes/{node}/network") or []
            return [V.slim_network(n) for n in raw]
        return await self._list(
            agent_context, client, "list_networks", "networks",
            params, {"node": node}, _rows, max_results,
        )

    async def _do_list_pools(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            raw = await client.get("/pools") or []
            return [{"poolid": p.get("poolid"), "comment": p.get("comment")} for p in raw]
        return await self._list(
            agent_context, client, "list_pools", "pools",
            params, {}, _rows, max_results,
        )

    async def _do_recent_tasks(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        node = (params.get("node") or "").strip()
        if node:
            raw = await client.get(
                f"/nodes/{node}/tasks", limit=max_results, source="all"
            ) or []
        else:
            raw = await client.get("/cluster/tasks") or []
        rows = [V.slim_task(t) for t in raw]
        return await self._tabular(
            agent_context, "recent_tasks", rows[:max_results], params
        )

    # ── Single-object actions ────────────────────────────────────────────────

    async def _do_get_node(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        node = _require(params, "node")
        status = await client.get(f"/nodes/{node}/status") or {}
        return {"node": node, "detail": V.slim_node_status(status)}

    async def _do_get_vm(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        config = await client.get(f"/nodes/{node}/{kind}/{vmid}/config") or {}
        status = await client.get(f"/nodes/{node}/{kind}/{vmid}/status/current") or {}
        return {"vm": V.slim_guest_config(kind, vmid, node, config, status)}

    async def _do_list_snapshots(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        raw = await client.get(f"/nodes/{node}/{kind}/{vmid}/snapshot") or []
        snaps = [V.slim_snapshot(s) for s in raw if s.get("name") != "current"]
        return {
            "vmid": vmid, "kind": kind, "node": node,
            "snapshot_count": len(snaps), "snapshots": snaps,
        }

    async def _do_get_vm_metrics(
        self, client: ProxmoxClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        status = await client.get(f"/nodes/{node}/{kind}/{vmid}/status/current") or {}
        return {"node": node, "metrics": V.slim_guest_metrics(kind, vmid, status)}


_ = Any
