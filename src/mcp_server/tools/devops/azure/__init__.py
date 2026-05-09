"""gSage AI — Azure management tools.

Five MCP tools share this package and the ``azure`` config namespace:

- :class:`azure_inventory.AzureInventoryTool` — read-only inventory of
  subscriptions, resource groups, VMs, disks, IPs, NICs, SQL servers/DBs,
  App Services, AKS clusters, storage accounts and snapshots, plus an
  ``orphans`` aggregator and ``describe_resource``.
- :class:`azure_metrics.AzureMetricsTool` — Azure Monitor metric queries
  for VMs, disks, App Services and SQL DBs, plus a power-state derived
  ``uptime`` view from the Activity Log.
- :class:`azure_costs.AzureCostsTool` — Azure Cost Management queries
  (current month / history / breakdowns) combined with Azure Advisor
  recommendations and gSage's own cost-saving heuristics.
- :class:`azure_dashboard.AzureDashboardTool` — curated managerial views
  composed from the read-only primitives above.
- :class:`azure_manage.AzureManageTool` — write actions gated by HITL
  approval (start/stop/restart/resize VMs, update tags, schedule
  shutdown).

Authentication is per-profile via Service Principal (``tenant_id``,
``client_id``, ``client_secret``) plus a ``default_subscription_id``.
The ``params.subscription_id`` field overrides the default per call.

A shared Redis cache (TTL 300s, key scoped by org/user/profile/sub) is
used for expensive list and metric queries; ``params.force_refresh=true``
bypasses it.
"""
