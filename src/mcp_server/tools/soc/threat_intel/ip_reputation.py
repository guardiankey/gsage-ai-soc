"""gSage AI — IP Reputation tool stub (superseded by threat_intel_lookup)."""

from __future__ import annotations

from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext


class IPReputationTool(BaseTool):
    """
    IP Reputation — query threat intelligence feeds for an IP address.

    Planned integrations: AbuseIPDB, GreyNoise, Shodan, AlienVault OTX.
    Returns: abuse confidence score, attack categories, last seen timestamp,
    country, ISP, open ports (from Shodan), CVEs (if applicable).

    Designed as a pluggable aggregator — each configured provider contributes
    to a composite score. Missing API keys silently skip that provider.

    STUB: superseded by ``threat_intel_lookup`` (VirusTotal + AbuseIPDB implemented).
    Kept for future expansion to GreyNoise, Shodan, and AlienVault OTX.

    Permission: ``threat:intel``
    """

    name: ClassVar[str] = "ip_reputation"
    version: ClassVar[str] = "0.1.0"
    summary: ClassVar[str] = "Query threat intelligence feeds to check an IP address's reputation and maliciousness score"
    category: ClassVar[str] = "threat_intel"
    core_tool: ClassVar[bool] = False
    available: ClassVar[bool] = False  # stub — requires threat-intel API keys
    permissions: ClassVar[list[str]] = ["threat:intel"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 15
    use_circuit_breaker: ClassVar[bool] = True

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["ip"],
        "properties": {
            "ip": {
                "type": "string",
                "description": "IPv4 or IPv6 address to query for reputation information.",
            },
            "providers": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["abuseipdb", "greynoise", "shodan", "otx"],
                },
                "description": (
                    "Limit lookup to specific providers. "
                    "Defaults to all configured providers."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "abuseipdb_key": {
            "type": "string",
            "description": "AbuseIPDB API key (encrypted)",
            "sensitive": True,
        },
        "greynoise_key": {
            "type": "string",
            "description": "GreyNoise API key (encrypted)",
            "sensitive": True,
        },
        "shodan_key": {
            "type": "string",
            "description": "Shodan API key (encrypted)",
            "sensitive": True,
        },
        "otx_key": {
            "type": "string",
            "description": "AlienVault OTX API key (encrypted)",
            "sensitive": True,
        },
    }
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """
        Params:
            ip (str, required): IPv4 or IPv6 address to query.
            providers (list[str], optional): Limit to specific providers
                (e.g., ["abuseipdb", "greynoise"]). Defaults to all configured.
        """
        raise NotImplementedError(
            "IPReputationTool is not yet implemented. "
            "Requires at least one configured threat-intel API key "
            "(AbuseIPDB, GreyNoise, Shodan, or OTX)."
        )
