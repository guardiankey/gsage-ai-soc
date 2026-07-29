"""gSage AI — shared config schema and client factory for the Wazuh tools.

Both ``wazuh_query`` (read) and ``wazuh_manage`` (write) consume the same
per-org configuration (manager URL + API credentials), so the schema, defaults
and the :func:`build_client` factory live here to avoid duplication.
"""

from __future__ import annotations

from typing import Any

from src.mcp_server.tools.soc.monitoring.wazuh._client import WazuhClient, WazuhError

# JSON-Schema-style config contract, stored per-org (encrypted) as a
# GSageToolConfig row.  ``supports_multiple_configs`` means one row per Wazuh
# manager (profile_id = "prod", "client-a", …).
WAZUH_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["url", "username", "password"],
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "Wazuh Manager API base URL including port, e.g. "
                "https://wazuh.example.com:55000. Must be reachable from the "
                "mcp-server container."
            ),
        },
        "username": {
            "type": "string",
            "description": "Wazuh API user (e.g. 'wazuh' or 'wazuh-wui').",
        },
        "password": {
            "type": "string",
            "description": "Wazuh API password.",
            "sensitive": True,
        },
        "verify_tls": {
            "type": "boolean",
            "description": (
                "Verify the server TLS certificate (default: true). Wazuh "
                "ships a self-signed certificate by default, so set this to "
                "false for out-of-the-box deployments."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "HTTP request timeout in seconds (default: 30).",
        },
    },
    "additionalProperties": False,
}

WAZUH_CONFIG_DEFAULTS: dict = {
    "verify_tls": True,
    "timeout": 30,
}


def build_client(config: dict[str, Any]) -> WazuhClient:
    """Instantiate a :class:`WazuhClient` from an effective tool config dict.

    Raises :class:`WazuhError` (via the client constructor) when required
    fields are missing, so callers can surface a clean ``CONFIG_MISSING`` /
    ``INVALID_CONFIG`` error.
    """
    return WazuhClient(
        url=str(config.get("url", "")),
        username=str(config.get("username", "")),
        password=str(config.get("password", "")),
        verify_tls=bool(config.get("verify_tls", True)),
        timeout=int(config.get("timeout", 30)),
    )


__all__ = [
    "WAZUH_CONFIG_SCHEMA",
    "WAZUH_CONFIG_DEFAULTS",
    "build_client",
    "WazuhError",
]
