"""gSage AI — Proxmox VE management tools.

Three MCP tools share this package and the ``proxmox`` config namespace:

- :class:`proxmox_inventory.ProxmoxInventoryTool` — read-only inventory of
  nodes, cluster status, QEMU VMs and LXC containers (+ merged config &
  live status), templates, storage, networks, pools, snapshots, recent
  tasks and per-guest perf metrics.
- :class:`proxmox_dashboard.ProxmoxDashboardTool` — aggregated read-only
  dashboards (cluster overview, node health, guest health, storage
  summary, capacity/overcommit report) composed from the inventory
  primitives.
- :class:`proxmox_manage.ProxmoxManageTool` — write actions gated by HITL
  approval: clone-from-template (with cloud-init / LXC customization),
  edit, power ops, snapshots, migrate, convert-to-template and delete.

Authentication is via a Proxmox **API token**
(``token_id`` = ``user@realm!tokenname`` + ``token_secret``). The client
talks to the REST API at ``https://{host}:8006/api2/json`` with ``httpx``
— fully async, no extra dependency.

**Multiple clusters**: the top-level config fields define the primary
(``default``) cluster; add more under the ``profiles`` config map (each
key is a cluster name with the same fields). Callers pick one with the
``profile`` param (omit it for ``default``).

Guests are addressed by ``vmid`` (unambiguous) or ``name`` (unique
cluster-wide); the node is resolved automatically from
``/cluster/resources``. Async worker tasks (UPID) are awaited
transparently.

A shared Redis cache (TTL 300s, key scoped by org/user/profile/host) is
used for expensive list queries; ``params.force_refresh=true`` bypasses it.

Tool classes are auto-discovered by ``build_registry()`` — no manual
registration is required.

Note: Proxmox treats a template as a one-way flag — there is no
template→VM conversion. The workflow is to clone a new guest from the
template (``clone_from_template``).
"""
