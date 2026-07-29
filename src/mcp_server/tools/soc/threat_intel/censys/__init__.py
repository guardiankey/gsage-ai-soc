"""gSage AI — Censys threat-intelligence integration.

Wraps the Censys Search API v2 (``https://search.censys.io/api/v2``) behind a
thin async :mod:`httpx` client.  Authentication is HTTP Basic using the
account **API ID** and **API Secret**.

The base URL is configurable, so an organisation migrated to the newer Censys
Platform host can point the tool there without a code change.

Tool
----
Read tool (no approval, ``threat:intel``):

* ``censys_lookup`` — host detail view, host search (Censys query language),
  and field aggregation (report).

Configuration is multi-tenant: the tool declares
``supports_multiple_configs=True`` and reads credentials from the admin
console (one ``GSageToolConfig`` row per Censys account).

Shared async client: :mod:`._client` (``CensysClient``, ``CensysError``).
"""
