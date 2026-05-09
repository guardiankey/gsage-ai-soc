"""gSage AI — Kubernetes integration tools.

Three MCP tools share this package:

- :class:`k8s_observe.K8sObserveTool` — read-only inspection of clusters
  (pods, deployments, events, logs, rollout status, resource usage).
- :class:`k8s_manage.K8sManageTool` — write operations gated by HITL
  approval (rollout restart, scale deployment, delete pod).
- :class:`k8s_dashboard.K8sDashboardTool` — managerial aggregations
  (cluster overview, workload health, top restarts, pending pods,
  recent warnings, resource pressure, namespace deep-dive).

All three tools share configuration namespace ``kubernetes`` and support
multiple clusters via :class:`gsage_tool_config.GSageToolConfig` profiles.

Authentication: discrete fields — ``api_server`` URL plus a service
account ``token`` (Bearer) and optional ``ca_cert`` (PEM). When running
inside the cluster, set ``in_cluster=true`` to use the pod's mounted
service account credentials.
"""
