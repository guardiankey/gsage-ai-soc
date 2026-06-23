"""gSage AI — SentinelOne EDR tools.

Three MCP tools share this package and the ``sentinelone`` config
namespace (mirroring the GravityZone EDR family's read/read/write split):

- :class:`s1_endpoints.S1EndpointsTool` — read-only agent inventory:
  list / get agents, agent activities, groups, sites.
- :class:`s1_threats.S1ThreatsTool` — read-only detections: list / get
  threats, analyst notes, hash blocklist.
- :class:`s1_management.S1ManagementTool` — approval-gated response:
  isolate / reconnect / scan agents, mitigate threats (kill / quarantine /
  remediate / rollback / un-quarantine), set analyst verdict, add notes,
  and manage the hash blocklist.

Authentication is per-profile via a SentinelOne **API token** sent as
``Authorization: ApiToken {token}`` against the console at
``{console_url}/web/api/v2.1``. The client uses ``httpx`` — fully async,
no extra dependency — with cursor pagination handled transparently.

Permissions use vendor-specific tags ``sentinelone:read`` /
``sentinelone:write`` (consistent with the GravityZone ``gravityzone:*``
convention). Agents are addressed by ``agent_id`` or unique
``computer_name``; site scope falls back to the profile's
``default_site_ids``.

Tool classes are auto-discovered by ``build_registry()`` — no manual
registration is required.
"""
