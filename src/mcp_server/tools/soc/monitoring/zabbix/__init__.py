"""gSage AI — Zabbix monitoring tools.

Provides a single tool for querying a Zabbix server in read-only mode:

    zabbix_query — inventory (hosts, groups, items, templates, interfaces,
                   maintenance), health/events (problems, triggers, events,
                   severity summary, consolidated host health) and metric
                   history.

Shared async client: :mod:`._client` (ZabbixClient, ZabbixError).

Authentication is performed via API token (preferred) or user/password
fallback.  Multiple Zabbix instances per organisation are supported via
``supports_multiple_configs=True`` (one GSageToolConfig row per profile).
"""
