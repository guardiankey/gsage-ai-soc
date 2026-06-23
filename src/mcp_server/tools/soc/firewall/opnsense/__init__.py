"""gSage AI — OPNsense firewall tools.

Two MCP tools share this package and the ``opnsense`` config namespace:

- :class:`opnsense_firewall.OPNsenseFirewallTool` — read-only triage:
  aliases (+ live entries), filter rules, firewall log, live state table,
  Suricata IDS alerts / status, gateway health, DHCP leases, ARP table and
  service states.
- :class:`opnsense_manage.OPNsenseManageTool` — write actions gated by HITL
  approval: block / unblock an IP via a firewall alias (with active-state
  drop), manage filter rules, toggle Suricata rules, and restart services.

Authentication is per-profile via an OPNsense **API key + secret** (sent as
HTTP Basic credentials), so multiple firewalls can be configured as
distinct profiles. The client talks to ``https://{host}/api`` with
``httpx`` — fully async, no extra dependency.

Permissions reuse the shared ``firewall`` category tags (``firewall:read``
/ ``firewall:write``) so the same grant governs the existing ``block_ip``
response tool and these OPNsense tools.

``block_ip`` / ``unblock_ip`` operate on a firewall alias (default from the
profile's ``block_alias``); that alias must already exist in OPNsense and
be referenced by a block rule for the change to take effect. A short Redis
cache (TTL 60s) backs the cacheable list reads; live reads (logs, states,
IDS alerts) are never cached.

Tool classes are auto-discovered by ``build_registry()`` — no manual
registration is required.
"""
