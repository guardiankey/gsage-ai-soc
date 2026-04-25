"""gSage AI — BitDefender GravityZone API tools.

Provides async tools for querying the BitDefender GravityZone API
(JSON-RPC over HTTPS with Basic Auth).

Tools:
    gz_endpoints  — Network read: list endpoints (v1.1), get endpoint details (v1.0)
    gz_security   — Security read: blocklist items (v1.2), PHASR recommendations/resources/identities (v1.0)
    gz_management — All write operations: blocklist, endpoint isolation, incident management (requires approval)

Shared client: :mod:`._client` (GravityZoneClient + GravityZoneError)
"""
