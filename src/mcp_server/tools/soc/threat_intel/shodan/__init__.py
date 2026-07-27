"""gSage AI — Shodan threat-intelligence integration.

Wraps the Shodan REST API (``https://api.shodan.io``) behind a thin async
:mod:`httpx` client.  Authentication is a single API key passed as the
``key`` query parameter.

Tool
----
Read tool (no approval, ``threat:intel``):

* ``shodan_lookup`` — host banner/service enrichment, host search & count,
  and DNS resolve/reverse/domain lookups.

Configuration is multi-tenant: the tool declares
``supports_multiple_configs=True`` and reads the API key from the admin
console (one ``GSageToolConfig`` row per Shodan account).

Shared async client: :mod:`._client` (``ShodanClient``, ``ShodanError``).
"""
