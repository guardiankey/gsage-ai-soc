"""gSage AI — VMware vCenter write actions (audited, approval-gated).

Handles the operational tasks analysts and ops need to perform on
vSphere VMs and templates. Every call requires a ``reason`` string for
the audit log and is subject to the platform approval workflow
(``requires_approval=True``).

Actions:

- ``create_vm_from_template`` — clone a template into a new VM, with
  optional placement (cluster / host / datastore / resource_pool /
  folder) and optional guest customization (hostname / static IP).
- ``edit_vm``                  — reconfigure a VM: vCPU, RAM, grow a disk,
  set annotation.
- ``clone_vm``                 — clone an existing VM to a new VM.
- ``vm_to_template``           — mark a VM as a template in place; pass
  ``new_name`` to instead clone into a new template, preserving the source.
- ``template_to_vm``           — convert a template back into a VM.
- ``power_on`` / ``power_off`` / ``reset`` / ``suspend`` — power ops.
- ``create_snapshot`` / ``revert_snapshot`` / ``delete_snapshot``.
- ``migrate_vm``               — vMotion to another host / datastore /
  resource pool.
- ``delete_vm``                — destroy a VM (high-risk; approval + reason).

Permission: ``vcenter:write``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.vmware._client import (
    VCENTER_CONFIG_DEFAULTS,
    VCENTER_CONFIG_SCHEMA,
    VCenterClient,
    VCenterError,
    build_vcenter_client,
)
from src.mcp_server.tools.devops.vmware import _views as V
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "create_vm_from_template",
    "edit_vm",
    "clone_vm",
    "vm_to_template",
    "template_to_vm",
    "power_on",
    "power_off",
    "reset",
    "suspend",
    "create_snapshot",
    "revert_snapshot",
    "delete_snapshot",
    "migrate_vm",
    "delete_vm",
})


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if not val:
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


class VCenterManageTool(BaseTool):
    """Approval-gated write operations on VMware vCenter VMs and templates.

    Every action requires a free-form ``reason`` for audit and is subject
    to the platform approval workflow.

    Permission: ``vcenter:write``.
    """

    name: ClassVar[str] = "vcenter_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Approval-gated vCenter management: create-from-template, edit, "
        "clone, VM<->template, power, snapshots, vMotion, delete."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "vcenter"
    permissions: ClassVar[list[str]] = ["vcenter:write"]
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
        "required": ["action", "name", "reason"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which write operation to perform.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "Name of the configured vCenter to act on (a key under "
                    "the config 'profiles' map). Omit (or 'default') for the "
                    "primary vCenter."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Primary target name: the VM/template to act on, or — for "
                    "create_vm_from_template / clone_vm — the SOURCE "
                    "template/VM to clone from."
                ),
            },
            "reason": {
                "type": "string",
                "minLength": 5,
                "description": (
                    "Free-form justification recorded in the audit log."
                ),
            },
            "new_name": {
                "type": "string",
                "description": (
                    "[create_vm_from_template, clone_vm, vm_to_template "
                    "(clone=true)] Name of the new VM / template to create."
                ),
            },
            "cluster": {
                "type": "string",
                "description": (
                    "[create_vm_from_template, clone_vm, template_to_vm] "
                    "Target cluster (its root resource pool is used unless "
                    "resource_pool is given)."
                ),
            },
            "host": {
                "type": "string",
                "description": (
                    "[create_vm_from_template, clone_vm, template_to_vm, "
                    "migrate_vm] Target ESXi host."
                ),
            },
            "datastore": {
                "type": "string",
                "description": (
                    "[create_vm_from_template, clone_vm, migrate_vm] Target "
                    "datastore."
                ),
            },
            "resource_pool": {
                "type": "string",
                "description": (
                    "[create_vm_from_template, clone_vm, template_to_vm, "
                    "migrate_vm] Target resource pool (overrides cluster's "
                    "root pool)."
                ),
            },
            "folder": {
                "type": "string",
                "description": (
                    "[create_vm_from_template, clone_vm] Target VM folder. "
                    "Defaults to the source's folder."
                ),
            },
            "power_on": {
                "type": "boolean",
                "description": (
                    "[create_vm_from_template, clone_vm] Power on the new VM "
                    "after creation (default false)."
                ),
            },
            "customize_hostname": {
                "type": "string",
                "description": (
                    "[create_vm_from_template] Guest hostname to apply "
                    "(triggers Linux/Windows guest customization)."
                ),
            },
            "customize_ip": {
                "type": "string",
                "description": (
                    "[create_vm_from_template] Static IPv4 for the first NIC. "
                    "Omit for DHCP. Requires customize_netmask + "
                    "customize_gateway."
                ),
            },
            "customize_netmask": {
                "type": "string",
                "description": "[create_vm_from_template] Subnet mask for customize_ip.",
            },
            "customize_gateway": {
                "type": "string",
                "description": "[create_vm_from_template] Default gateway for customize_ip.",
            },
            "num_cpu": {
                "type": "integer",
                "minimum": 1,
                "maximum": 512,
                "description": "[edit_vm] New vCPU count.",
            },
            "memory_mb": {
                "type": "integer",
                "minimum": 64,
                "description": "[edit_vm] New memory size in MB.",
            },
            "disk_label": {
                "type": "string",
                "description": (
                    "[edit_vm] Label of the disk to grow (e.g. 'Hard disk 1')."
                ),
            },
            "disk_size_gb": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "[edit_vm] New size in GB for disk_label. Grow only — "
                    "vSphere forbids shrinking a virtual disk."
                ),
            },
            "annotation": {
                "type": "string",
                "description": "[edit_vm] Set the VM notes/annotation field.",
            },
            "snapshot_name": {
                "type": "string",
                "description": (
                    "[create_snapshot, revert_snapshot, delete_snapshot] "
                    "Snapshot name."
                ),
            },
            "snapshot_description": {
                "type": "string",
                "description": "[create_snapshot] Optional snapshot description.",
            },
            "snapshot_memory": {
                "type": "boolean",
                "description": (
                    "[create_snapshot] Include the VM's memory state "
                    "(default false)."
                ),
            },
            "quiesce": {
                "type": "boolean",
                "description": (
                    "[create_snapshot] Quiesce the guest filesystem via "
                    "VMware Tools (default false)."
                ),
            },
            "remove_children": {
                "type": "boolean",
                "description": (
                    "[delete_snapshot] Also remove child snapshots "
                    "(default false)."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = VCENTER_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = VCENTER_CONFIG_DEFAULTS
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
            async with build_vcenter_client(
                config, profile=params.get("profile")
            ) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, agent_context)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except VCenterError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT")
            return self._failure(
                exc.code, str(exc), retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("vcenter_manage(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Placement resolution helpers ─────────────────────────────────────────

    async def _resolve_resource_pool(
        self, client: VCenterClient, params: dict
    ) -> Any:
        """Resolve a resource pool from ``resource_pool`` or ``cluster``."""
        if (rp := (params.get("resource_pool") or "").strip()):
            return await client.get_obj("ResourcePool", rp)
        if (cluster := (params.get("cluster") or "").strip()):
            cl = await client.get_obj("ClusterComputeResource", cluster)
            return await client.call(lambda: cl.resourcePool)
        return None

    async def _build_relocate_spec(
        self, client: VCenterClient, params: dict
    ) -> Any:
        """Build a vim.vm.RelocateSpec from optional placement params."""
        vim = client.vim
        spec = vim.vm.RelocateSpec()
        if (pool := await self._resolve_resource_pool(client, params)) is not None:
            spec.pool = pool
        if (host := (params.get("host") or "").strip()):
            spec.host = await client.get_obj("HostSystem", host)
        if (ds := (params.get("datastore") or "").strip()):
            spec.datastore = await client.get_obj("Datastore", ds)
        return spec

    async def _build_customization(
        self, client: VCenterClient, params: dict
    ) -> Any:
        """Build a vim.vm.customization.Specification, or None.

        Applies a guest hostname and, when a static IP is supplied, a fixed
        IPv4 on the first NIC. Uses a Linux prep by default; the agent can
        target Windows guests by relying on the template's own sysprep.
        """
        hostname = (params.get("customize_hostname") or "").strip()
        ip = (params.get("customize_ip") or "").strip()
        if not hostname and not ip:
            return None
        vim = client.vim
        cust = vim.vm.customization
        spec = cust.Specification()

        # Identity (Linux prep — broadly compatible; hostname is the key bit).
        ident = cust.LinuxPrep()
        ident.domain = ""
        ident.hostName = cust.FixedName(name=hostname or (params.get("new_name") or "vm"))
        spec.identity = ident
        spec.globalIPSettings = cust.GlobalIPSettings()

        # NIC settings.
        adapter = cust.IPSettings()
        if ip:
            netmask = _require(params, "customize_netmask")
            gateway = _require(params, "customize_gateway")
            adapter.ip = cust.FixedIp(ipAddress=ip)
            adapter.subnetMask = netmask
            adapter.gateway = [gateway]
        else:
            adapter.ip = cust.DhcpIpGenerator()
        nic_map = cust.AdapterMapping(adapter=adapter)
        spec.nicSettingMap = [nic_map]
        return spec

    # ── Clone / template actions ─────────────────────────────────────────────

    async def _do_create_vm_from_template(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        return await self._clone(client, params, as_template=False, from_template=True)

    async def _do_clone_vm(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        return await self._clone(client, params, as_template=False, from_template=False)

    async def _clone(
        self,
        client: VCenterClient,
        params: dict,
        *,
        as_template: bool,
        from_template: bool,
    ) -> dict:
        source = await client.find_vm(name=_require(params, "name"))
        new_name = _require(params, "new_name")
        vim = client.vim

        relocate = await self._build_relocate_spec(client, params)
        clone_spec = vim.vm.CloneSpec(
            location=relocate,
            powerOn=bool(params.get("power_on")) and not as_template,
            template=as_template,
        )
        if from_template:
            customization = await self._build_customization(client, params)
            if customization is not None:
                clone_spec.customization = customization

        # Target folder: explicit, else the source's parent folder.
        if (folder_name := (params.get("folder") or "").strip()):
            folder = await client.get_obj("Folder", folder_name)
        else:
            folder = await client.call(lambda: source.parent)

        log.info(
            "vcenter_manage %s source=%s new=%s reason=%r",
            "clone_template" if from_template else "clone_vm",
            params.get("name"), new_name, params.get("reason"),
        )
        task = await client.call(
            lambda: source.Clone(folder=folder, name=new_name, spec=clone_spec)
        )
        result = await client.wait_for_task(task)
        return {
            "source": params.get("name"),
            "new_name": new_name,
            "is_template": as_template,
            "moid": V._moid(result) if result is not None else None,
            "powered_on": bool(clone_spec.powerOn),
        }

    async def _do_vm_to_template(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        if (params.get("new_name") or "").strip():
            # new_name given → clone into a NEW template, preserving the
            # source VM. Otherwise mark the VM itself as a template in place.
            return await self._clone(
                client, params, as_template=True, from_template=False
            )
        vm = await client.find_vm(name=_require(params, "name"))
        log.info(
            "vcenter_manage vm_to_template vm=%s reason=%r",
            params.get("name"), params.get("reason"),
        )
        # MarkAsTemplate is synchronous (no Task).
        await client.call(lambda: vm.MarkAsTemplate())
        return {"name": params.get("name"), "is_template": True}

    async def _do_template_to_vm(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        template = await client.find_vm(name=_require(params, "name"))
        pool = await self._resolve_resource_pool(client, params)
        if pool is None:
            raise _ParamError(
                "template_to_vm requires 'resource_pool' or 'cluster' to "
                "place the resulting VM."
            )
        host_obj = None
        if (host := (params.get("host") or "").strip()):
            host_obj = await client.get_obj("HostSystem", host)
        log.info(
            "vcenter_manage template_to_vm template=%s reason=%r",
            params.get("name"), params.get("reason"),
        )
        # MarkAsVirtualMachine is synchronous (no Task).
        await client.call(lambda: template.MarkAsVirtualMachine(pool=pool, host=host_obj))
        return {"name": params.get("name"), "is_template": False}

    # ── Reconfigure ──────────────────────────────────────────────────────────

    async def _do_edit_vm(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        vim = client.vim
        spec = vim.vm.ConfigSpec()
        changed: list[str] = []

        if (num_cpu := params.get("num_cpu")) is not None:
            spec.numCPUs = int(num_cpu)
            changed.append(f"num_cpu={num_cpu}")
        if (mem := params.get("memory_mb")) is not None:
            spec.memoryMB = int(mem)
            changed.append(f"memory_mb={mem}")
        if (annotation := params.get("annotation")) is not None:
            spec.annotation = str(annotation)
            changed.append("annotation")

        disk_label = (params.get("disk_label") or "").strip()
        disk_size_gb = params.get("disk_size_gb")
        if disk_label and disk_size_gb is not None:
            dev_change = await self._build_disk_grow(
                client, vm, disk_label, int(disk_size_gb)
            )
            spec.deviceChange = [dev_change]
            changed.append(f"{disk_label}->{disk_size_gb}GB")

        if not changed:
            raise _ParamError(
                "edit_vm requires at least one of: num_cpu, memory_mb, "
                "annotation, or disk_label+disk_size_gb."
            )

        log.info(
            "vcenter_manage edit_vm vm=%s changes=%s reason=%r",
            params.get("name"), changed, params.get("reason"),
        )
        task = await client.call(lambda: vm.ReconfigVM_Task(spec=spec))
        await client.wait_for_task(task)
        return {"name": params.get("name"), "changes": changed}

    async def _build_disk_grow(
        self, client: VCenterClient, vm: Any, disk_label: str, new_gb: int
    ) -> Any:
        """Build a VirtualDeviceConfigSpec that grows an existing disk."""
        vim = client.vim
        new_kb = new_gb * 1024 * 1024

        def _find_disk() -> Any:
            for dev in vm.config.hardware.device:
                if hasattr(dev, "capacityInKB") and dev.deviceInfo.label == disk_label:
                    return dev
            return None

        disk = await client.call(_find_disk)
        if disk is None:
            raise _ParamError(f"Disk {disk_label!r} not found on the VM.")
        current_kb = await client.call(lambda: disk.capacityInKB)
        if new_kb <= current_kb:
            raise _ParamError(
                f"disk_size_gb must be larger than the current size "
                f"({round(current_kb / 1024 / 1024, 2)} GB); vSphere cannot "
                "shrink a virtual disk."
            )
        dev_spec = vim.vm.device.VirtualDeviceSpec()
        dev_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        dev_spec.device = disk
        disk.capacityInKB = new_kb
        return dev_spec

    # ── Power ops ────────────────────────────────────────────────────────────

    async def _do_power_on(self, client, params, agent_context) -> dict:
        return await self._power(client, params, "PowerOnVM_Task", "poweredOn")

    async def _do_power_off(self, client, params, agent_context) -> dict:
        return await self._power(client, params, "PowerOffVM_Task", "poweredOff")

    async def _do_reset(self, client, params, agent_context) -> dict:
        return await self._power(client, params, "ResetVM_Task", "reset")

    async def _do_suspend(self, client, params, agent_context) -> dict:
        return await self._power(client, params, "SuspendVM_Task", "suspended")

    async def _power(
        self, client: VCenterClient, params: dict, method: str, target: str,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        log.info(
            "vcenter_manage %s vm=%s reason=%r",
            method, params.get("name"), params.get("reason"),
        )
        task = await client.call(lambda: getattr(vm, method)())
        await client.wait_for_task(task)
        return {"name": params.get("name"), "power_state": target}

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def _do_create_snapshot(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        snap_name = _require(params, "snapshot_name")
        desc = params.get("snapshot_description") or ""
        memory = bool(params.get("snapshot_memory"))
        quiesce = bool(params.get("quiesce"))
        log.info(
            "vcenter_manage create_snapshot vm=%s snap=%s reason=%r",
            params.get("name"), snap_name, params.get("reason"),
        )
        task = await client.call(
            lambda: vm.CreateSnapshot_Task(
                name=snap_name, description=desc, memory=memory, quiesce=quiesce
            )
        )
        await client.wait_for_task(task)
        return {"name": params.get("name"), "snapshot": snap_name, "memory": memory}

    async def _do_revert_snapshot(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        snap_name = _require(params, "snapshot_name")
        snap = await self._find_snapshot(client, vm, snap_name)
        log.info(
            "vcenter_manage revert_snapshot vm=%s snap=%s reason=%r",
            params.get("name"), snap_name, params.get("reason"),
        )
        task = await client.call(lambda: snap.RevertToSnapshot_Task())
        await client.wait_for_task(task)
        return {"name": params.get("name"), "reverted_to": snap_name}

    async def _do_delete_snapshot(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        snap_name = _require(params, "snapshot_name")
        remove_children = bool(params.get("remove_children"))
        snap = await self._find_snapshot(client, vm, snap_name)
        log.info(
            "vcenter_manage delete_snapshot vm=%s snap=%s children=%s reason=%r",
            params.get("name"), snap_name, remove_children, params.get("reason"),
        )
        task = await client.call(
            lambda: snap.RemoveSnapshot_Task(removeChildren=remove_children)
        )
        await client.wait_for_task(task)
        return {
            "name": params.get("name"),
            "deleted": snap_name,
            "removed_children": remove_children,
        }

    async def _find_snapshot(
        self, client: VCenterClient, vm: Any, snap_name: str
    ) -> Any:
        """Locate a snapshot managed object by name within a VM's tree."""

        def _search() -> Any:
            snap_info = vm.snapshot
            if snap_info is None:
                return None
            stack = list(snap_info.rootSnapshotList or [])
            while stack:
                node = stack.pop()
                if node.name == snap_name:
                    return node.snapshot
                stack.extend(node.childSnapshotList or [])
            return None

        snap = await client.call(_search)
        if snap is None:
            raise VCenterError(
                f"Snapshot {snap_name!r} not found on VM.", code="NOT_FOUND"
            )
        return snap

    # ── Migrate (vMotion) ────────────────────────────────────────────────────

    async def _do_migrate_vm(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        if not any(
            (params.get(k) or "").strip()
            for k in ("host", "datastore", "resource_pool", "cluster")
        ):
            raise _ParamError(
                "migrate_vm requires at least one target: host, datastore, "
                "resource_pool or cluster."
            )
        relocate = await self._build_relocate_spec(client, params)
        log.info(
            "vcenter_manage migrate_vm vm=%s host=%s ds=%s reason=%r",
            params.get("name"), params.get("host"), params.get("datastore"),
            params.get("reason"),
        )
        task = await client.call(lambda: vm.RelocateVM_Task(spec=relocate))
        await client.wait_for_task(task)
        return {
            "name": params.get("name"),
            "target_host": params.get("host"),
            "target_datastore": params.get("datastore"),
        }

    # ── Delete ───────────────────────────────────────────────────────────────

    async def _do_delete_vm(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        log.warning(
            "vcenter_manage delete_vm vm=%s reason=%r",
            params.get("name"), params.get("reason"),
        )
        task = await client.call(lambda: vm.Destroy_Task())
        await client.wait_for_task(task)
        return {"name": params.get("name"), "destroyed": True}


_ = Any
