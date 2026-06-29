"""gSage AI — VMware vCenter / vSphere management tools.

Three MCP tools share this package and the ``vcenter`` config namespace:

- :class:`vcenter_inventory.VCenterInventoryTool` — read-only inventory of
  datacenters, clusters, hosts, VMs (+ full config), templates, datastores,
  networks, resource pools and folders, plus ``find_vm`` (by IP/name/UUID),
  snapshot listings, recent tasks/events and real-time VM perf metrics.
- :class:`vcenter_dashboard.VCenterDashboardTool` — aggregated read-only
  dashboards (cluster overview, host health, VM health, datastore summary,
  capacity/overcommit report) composed from the inventory primitives.
- :class:`vcenter_manage.VCenterManageTool` — write actions gated by HITL
  approval: create-from-template, edit, clone, VM↔template conversion,
  power ops, snapshots, vMotion and delete.

Authentication is via a vCenter user/password
(``host`` / ``user`` / ``password`` / ``port`` / ``verify_ssl``). The
underlying SDK is **pyVmomi** (imported lazily; see ``_client.py``).

**Multiple vCenters**: the top-level config fields define the primary
(``default``) vCenter; add more under the ``profiles`` config map (each
key is a vCenter name with the same fields). Callers pick one with the
``profile`` param (omit it for ``default``).

A shared Redis cache (TTL 300s, key scoped by org/user/profile/host) is
used for expensive list queries; ``params.force_refresh=true`` bypasses it.

Tool classes are auto-discovered by ``build_registry()`` — no manual
registration is required.
"""
