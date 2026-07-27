"""gSage AI — Wazuh SIEM/XDR integration tools.

Wraps the Wazuh Manager REST API (default port 55000) behind a thin async
facade built on :mod:`httpx`.  Authentication uses the Wazuh
``/security/user/authenticate`` endpoint (HTTP Basic → short-lived JWT),
after which every call carries an ``Authorization: Bearer <token>`` header.

Tools
-----

Read tool (no approval, ``wazuh:read``):

* ``wazuh_query`` — agents inventory & status, manager health, ruleset,
  Security Configuration Assessment (SCA) and File Integrity Monitoring
  (syscheck/FIM) results.

Write tool (approval-gated, ``wazuh:write``):

* ``wazuh_manage`` — operational response actions: run active-response
  commands on agents, restart agents, and add/remove agents from groups.
  ``requires_approval=True`` so the gSage agent layer collects human
  approval before execution (same HITL contract as ``block_ip``).

Configuration is multi-tenant: both tools declare
``supports_multiple_configs=True`` and pull credentials from the admin
console at run-time (one ``GSageToolConfig`` row per Wazuh manager).

Shared async client: :mod:`._client` (``WazuhClient``, ``WazuhError``).
"""
