"""gSage AI — Proxmox VE write actions (audited, approval-gated).

Operational tasks on Proxmox VE QEMU VMs and LXC containers. Every call
requires a ``reason`` for the audit log and is subject to the platform
approval workflow (``requires_approval=True``).

Actions:

- ``clone_from_template`` — clone a template (or any guest) into a new
  guest, with placement (target_node / storage / pool) and optional
  customization. QEMU uses cloud-init (ci_user / ci_password /
  ipconfig0 / ci_sshkeys); LXC uses hostname / password / net0.
- ``edit_vm``            — reconfigure: cores, memory, a network device,
  description; grow a disk via ``disk`` + ``disk_size`` (e.g. '+10G').
- ``start`` / ``stop`` / ``shutdown`` / ``reset`` / ``suspend`` /
  ``resume`` — power operations.
- ``create_snapshot`` / ``rollback_snapshot`` / ``delete_snapshot``.
- ``migrate_vm``        — migrate a guest to another node (online when
  possible; storage migration via ``with_local_disks``).
- ``convert_to_template`` — mark a guest as a template (one-way in
  Proxmox: there is no template→VM conversion; clone from it instead).
- ``delete_vm``          — destroy a guest (high-risk; approval + reason).

A guest is addressed by ``vmid`` (unambiguous) or ``name`` (must be
unique cluster-wide). Permission: ``proxmox:write``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.proxmox._client import (
    PROXMOX_CONFIG_DEFAULTS,
    PROXMOX_CONFIG_SCHEMA,
    ProxmoxClient,
    ProxmoxError,
    build_proxmox_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "clone_from_template",
    "edit_vm",
    "start",
    "stop",
    "shutdown",
    "reset",
    "suspend",
    "resume",
    "create_snapshot",
    "rollback_snapshot",
    "delete_snapshot",
    "migrate_vm",
    "convert_to_template",
    "delete_vm",
})

_POWER_VERBS = {
    "start": "start",
    "stop": "stop",
    "shutdown": "shutdown",
    "reset": "reset",
    "suspend": "suspend",
    "resume": "resume",
}


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


class ProxmoxManageTool(BaseTool):
    """Approval-gated write operations on Proxmox VE guests (QEMU + LXC).

    Every action requires a free-form ``reason`` for audit and is subject
    to the platform approval workflow.

    Permission: ``proxmox:write``.
    """

    name: ClassVar[str] = "proxmox_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Approval-gated Proxmox VE management: clone-from-template, edit, "
        "power, snapshots, migrate, convert-to-template, delete (QEMU+LXC)."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "proxmox"
    permissions: ClassVar[list[str]] = ["proxmox:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 600
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "name",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "reason"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which write operation to perform.",
            },
            "profile": {"type": "string"},
            "reason": {
                "type": "string",
                "minLength": 5,
                "description": (
                    "Free-form justification recorded in the audit log."
                ),
            },
            "vmid": {
                "type": "integer",
                "description": (
                    "Target guest VMID. For clone_from_template this is the "
                    "SOURCE template/guest. Use this or 'name'."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Target guest name (unique cluster-wide). Alternative "
                    "to 'vmid'."
                ),
            },
            "new_vmid": {
                "type": "integer",
                "description": (
                    "[clone_from_template] VMID for the new guest. Omit to "
                    "auto-allocate the cluster's next free id."
                ),
            },
            "new_name": {
                "type": "string",
                "description": (
                    "[clone_from_template] Name (QEMU) / hostname (LXC) of "
                    "the new guest."
                ),
            },
            "full": {
                "type": "boolean",
                "description": (
                    "[clone_from_template] Full clone (default true). False "
                    "= linked clone (template + same storage required)."
                ),
            },
            "target_node": {
                "type": "string",
                "description": (
                    "[clone_from_template, migrate_vm] Destination node."
                ),
            },
            "storage": {
                "type": "string",
                "description": (
                    "[clone_from_template] Target storage for the new guest's "
                    "disks (required for a full clone across storages)."
                ),
            },
            "pool": {
                "type": "string",
                "description": "[clone_from_template] Resource pool to add the new guest to.",
            },
            "power_on": {
                "type": "boolean",
                "description": (
                    "[clone_from_template] Start the new guest after cloning "
                    "(and after applying customization). Default false."
                ),
            },
            "ci_user": {
                "type": "string",
                "description": "[clone_from_template, QEMU] cloud-init default user.",
            },
            "ci_password": {
                "type": "string",
                "description": "[clone_from_template, QEMU] cloud-init user password.",
            },
            "ci_sshkeys": {
                "type": "string",
                "description": "[clone_from_template, QEMU] cloud-init SSH public key(s).",
            },
            "ipconfig0": {
                "type": "string",
                "description": (
                    "[clone_from_template] First NIC IP config. QEMU "
                    "cloud-init form: 'ip=10.0.0.5/24,gw=10.0.0.1' or "
                    "'ip=dhcp'."
                ),
            },
            "password": {
                "type": "string",
                "description": "[clone_from_template, LXC] root password for the container.",
            },
            "net0": {
                "type": "string",
                "description": (
                    "[clone_from_template (LXC), edit_vm] Network device "
                    "string, e.g. 'name=eth0,bridge=vmbr0,ip=dhcp'."
                ),
            },
            "cores": {
                "type": "integer",
                "minimum": 1,
                "maximum": 512,
                "description": "[edit_vm] New CPU core count.",
            },
            "memory_mb": {
                "type": "integer",
                "minimum": 16,
                "description": "[edit_vm] New memory size in MB.",
            },
            "description": {
                "type": "string",
                "description": "[edit_vm] Set the guest description/notes.",
            },
            "disk": {
                "type": "string",
                "description": (
                    "[edit_vm] Disk to grow (e.g. 'scsi0', 'virtio0', "
                    "'rootfs'). Requires disk_size."
                ),
            },
            "disk_size": {
                "type": "string",
                "description": (
                    "[edit_vm] Grow amount or absolute size, e.g. '+10G' "
                    "(add 10 GB) or '50G'. Grow only — Proxmox cannot shrink."
                ),
            },
            "snapshot_name": {
                "type": "string",
                "description": (
                    "[create_snapshot, rollback_snapshot, delete_snapshot] "
                    "Snapshot name."
                ),
            },
            "snapshot_description": {
                "type": "string",
                "description": "[create_snapshot] Optional description.",
            },
            "snapshot_vmstate": {
                "type": "boolean",
                "description": (
                    "[create_snapshot, QEMU] Include RAM state (default "
                    "false)."
                ),
            },
            "online": {
                "type": "boolean",
                "description": (
                    "[migrate_vm] Perform a live/online migration (default "
                    "true for running guests)."
                ),
            },
            "with_local_disks": {
                "type": "boolean",
                "description": (
                    "[migrate_vm, QEMU] Migrate local disks to the target "
                    "node's storage (default false)."
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
        try:
            async with build_proxmox_client(config) as client:
                if action in _POWER_VERBS:
                    data = await self._do_power(client, params, action)
                else:
                    handler = getattr(self, f"_do_{action}")
                    data = await handler(client, params, agent_context)
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
            log.exception("proxmox_manage(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Clone from template ──────────────────────────────────────────────────

    async def _do_clone_from_template(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, src_vmid = await _resolve_guest(client, params)
        new_vmid = params.get("new_vmid")
        if new_vmid in (None, ""):
            new_vmid = int(await client.get("/cluster/nextid"))
        new_vmid = int(new_vmid)
        new_name = (params.get("new_name") or "").strip()
        full = params.get("full")
        full = True if full is None else bool(full)

        clone_body: dict = {
            "newid": new_vmid,
            "full": 1 if full else 0,
            "target": (params.get("target_node") or "").strip() or None,
            "storage": (params.get("storage") or "").strip() or None,
            "pool": (params.get("pool") or "").strip() or None,
        }
        # QEMU uses 'name'; LXC uses 'hostname'.
        if new_name:
            clone_body["hostname" if kind == "lxc" else "name"] = new_name

        log.info(
            "proxmox_manage clone src=%s/%s new_vmid=%s reason=%r",
            kind, src_vmid, new_vmid, params.get("reason"),
        )
        upid = await client.post(
            f"/nodes/{node}/{kind}/{src_vmid}/clone", **clone_body
        )
        await client.run_task(node, upid)

        # The clone lands on target_node when given, else the source node.
        dest_node = clone_body["target"] or node

        applied = await self._apply_customization(
            client, dest_node, kind, new_vmid, params
        )

        powered_on = False
        if bool(params.get("power_on")):
            start_upid = await client.post(
                f"/nodes/{dest_node}/{kind}/{new_vmid}/status/start"
            )
            await client.run_task(dest_node, start_upid)
            powered_on = True

        return {
            "source_vmid": src_vmid,
            "new_vmid": new_vmid,
            "kind": kind,
            "node": dest_node,
            "full_clone": full,
            "customization_applied": applied,
            "powered_on": powered_on,
        }

    async def _apply_customization(
        self, client: ProxmoxClient, node: str, kind: str, vmid: int, params: dict,
    ) -> list[str]:
        """Apply post-clone customization via a config update. Returns keys set."""
        body: dict = {}
        if kind == "qemu":
            mapping = {
                "ci_user": "ciuser",
                "ci_password": "cipassword",
                "ci_sshkeys": "sshkeys",
                "ipconfig0": "ipconfig0",
            }
        else:  # lxc
            mapping = {
                "password": "password",
                "ipconfig0": None,  # not applicable to LXC
                "net0": "net0",
            }
        for pkey, ckey in mapping.items():
            if ckey is None:
                continue
            val = params.get(pkey)
            if val not in (None, ""):
                body[ckey] = val
        if not body:
            return []
        await client.post(f"/nodes/{node}/{kind}/{vmid}/config", **body)
        return sorted(body.keys())

    # ── Edit ─────────────────────────────────────────────────────────────────

    async def _do_edit_vm(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        body: dict = {}
        changed: list[str] = []

        if (cores := params.get("cores")) is not None:
            body["cores"] = int(cores)
            changed.append(f"cores={cores}")
        if (mem := params.get("memory_mb")) is not None:
            body["memory"] = int(mem)
            changed.append(f"memory={mem}MB")
        if (desc := params.get("description")) is not None:
            body["description"] = str(desc)
            changed.append("description")
        if (net0 := (params.get("net0") or "").strip()):
            body["net0"] = net0
            changed.append("net0")

        if body:
            await client.post(f"/nodes/{node}/{kind}/{vmid}/config", **body)

        disk = (params.get("disk") or "").strip()
        disk_size = (params.get("disk_size") or "").strip()
        if disk and disk_size:
            await client.put(
                f"/nodes/{node}/{kind}/{vmid}/resize", disk=disk, size=disk_size
            )
            changed.append(f"resize {disk} {disk_size}")
        elif disk or disk_size:
            raise _ParamError("disk and disk_size must be provided together.")

        if not changed:
            raise _ParamError(
                "edit_vm requires at least one of: cores, memory_mb, "
                "description, net0, or disk+disk_size."
            )

        log.info(
            "proxmox_manage edit %s/%s changes=%s reason=%r",
            kind, vmid, changed, params.get("reason"),
        )
        return {"vmid": vmid, "kind": kind, "node": node, "changes": changed}

    # ── Power ────────────────────────────────────────────────────────────────

    async def _do_power(
        self, client: ProxmoxClient, params: dict, action: str,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        verb = _POWER_VERBS[action]
        log.info(
            "proxmox_manage power %s %s/%s reason=%r",
            verb, kind, vmid, params.get("reason"),
        )
        upid = await client.post(f"/nodes/{node}/{kind}/{vmid}/status/{verb}")
        await client.run_task(node, upid)
        return {"vmid": vmid, "kind": kind, "node": node, "power_action": verb}

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def _do_create_snapshot(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        snap = _require(params, "snapshot_name")
        body: dict = {
            "snapname": snap,
            "description": params.get("snapshot_description") or None,
        }
        if kind == "qemu" and params.get("snapshot_vmstate") is not None:
            body["vmstate"] = 1 if params.get("snapshot_vmstate") else 0
        log.info(
            "proxmox_manage create_snapshot %s/%s snap=%s reason=%r",
            kind, vmid, snap, params.get("reason"),
        )
        upid = await client.post(f"/nodes/{node}/{kind}/{vmid}/snapshot", **body)
        await client.run_task(node, upid)
        return {"vmid": vmid, "kind": kind, "node": node, "snapshot": snap}

    async def _do_rollback_snapshot(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        snap = _require(params, "snapshot_name")
        log.info(
            "proxmox_manage rollback_snapshot %s/%s snap=%s reason=%r",
            kind, vmid, snap, params.get("reason"),
        )
        upid = await client.post(
            f"/nodes/{node}/{kind}/{vmid}/snapshot/{snap}/rollback"
        )
        await client.run_task(node, upid)
        return {"vmid": vmid, "kind": kind, "node": node, "rolled_back_to": snap}

    async def _do_delete_snapshot(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        snap = _require(params, "snapshot_name")
        log.info(
            "proxmox_manage delete_snapshot %s/%s snap=%s reason=%r",
            kind, vmid, snap, params.get("reason"),
        )
        upid = await client.delete(f"/nodes/{node}/{kind}/{vmid}/snapshot/{snap}")
        await client.run_task(node, upid)
        return {"vmid": vmid, "kind": kind, "node": node, "deleted": snap}

    # ── Migrate ──────────────────────────────────────────────────────────────

    async def _do_migrate_vm(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        target = _require(params, "target_node")
        if target == node:
            raise _ParamError(
                f"target_node {target!r} is the guest's current node."
            )
        body: dict = {"target": target}
        # 'online' (qemu) / 'restart' semantics differ; pass online when set.
        if params.get("online") is not None:
            body["online"] = 1 if params.get("online") else 0
        if kind == "qemu" and params.get("with_local_disks"):
            body["with-local-disks"] = 1
        log.info(
            "proxmox_manage migrate %s/%s %s->%s reason=%r",
            kind, vmid, node, target, params.get("reason"),
        )
        upid = await client.post(f"/nodes/{node}/{kind}/{vmid}/migrate", **body)
        await client.run_task(node, upid)
        return {"vmid": vmid, "kind": kind, "from_node": node, "to_node": target}

    # ── Convert to template ──────────────────────────────────────────────────

    async def _do_convert_to_template(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        log.info(
            "proxmox_manage convert_to_template %s/%s reason=%r",
            kind, vmid, params.get("reason"),
        )
        # Synchronous on the API (no UPID).
        await client.post(f"/nodes/{node}/{kind}/{vmid}/template")
        return {"vmid": vmid, "kind": kind, "node": node, "is_template": True}

    # ── Delete ───────────────────────────────────────────────────────────────

    async def _do_delete_vm(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        node, kind, vmid = await _resolve_guest(client, params)
        log.warning(
            "proxmox_manage delete %s/%s reason=%r",
            kind, vmid, params.get("reason"),
        )
        upid = await client.delete(f"/nodes/{node}/{kind}/{vmid}")
        await client.run_task(node, upid)
        return {"vmid": vmid, "kind": kind, "node": node, "destroyed": True}


_ = Any
