"""gSage AI — RDAP Lookup tool (MVP ⭐)."""

from __future__ import annotations

import ipaddress
import re
from typing import ClassVar, Optional

import httpx

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)

# IANA RDAP bootstrap endpoints
RDAP_DOMAIN_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"
RDAP_IP_BOOTSTRAP = "https://data.iana.org/rdap/ipv4.json"
RDAP_IPV6_BOOTSTRAP = "https://data.iana.org/rdap/ipv6.json"

# Well-known RDAP endpoints for common TLDs (fallback without bootstrap lookup)
RDAP_FALLBACK_ENDPOINTS = {
    "com": "https://rdap.verisign.com/com/v1/",
    "net": "https://rdap.verisign.com/net/v1/",
    "org": "https://rdap.org/",
    "io":  "https://rdap.nic.io/",
    "br":  "https://rdap.registro.br/",
    "uk":  "https://rdap.nominet.uk/uk/",
    "de":  "https://rdap.denic.de/",
    "fr":  "https://rdap.nic.fr/",
    "nl":  "https://rdap.sidn.nl/rdap/",
}


def _is_ip(target: str) -> bool:
    """Check if target is an IP address."""
    try:
        ipaddress.ip_address(target)
        return True
    except ValueError:
        return False


def _extract_rdap_domain_data(rdap: dict) -> dict:
    """Extract relevant fields from RDAP domain response."""
    data: dict = {"type": "domain"}

    # Basic handle and name
    data["handle"] = rdap.get("handle")
    data["ldhName"] = rdap.get("ldhName")
    data["unicodeName"] = rdap.get("unicodeName")

    # Status
    data["status"] = rdap.get("status", [])

    # Nameservers
    nameservers = rdap.get("nameservers", [])
    data["nameservers"] = [ns.get("ldhName") for ns in nameservers if ns.get("ldhName")]

    # Entities (registrar, registrant, etc.)
    entities = []
    for entity in rdap.get("entities", []):
        vcard = entity.get("vcardArray", [[], []])
        props = {p[0]: p[3] for p in vcard[1] if isinstance(p, list) and len(p) >= 4}
        entities.append({
            "roles": entity.get("roles", []),
            "name": props.get("fn"),
            "email": props.get("email"),
            "org": props.get("org"),
        })
    data["entities"] = entities

    # Events (registered, expiration, last changed)
    events = {}
    for event in rdap.get("events", []):
        action = event.get("eventAction")
        date = event.get("eventDate")
        if action and date:
            events[action] = date
    data["events"] = events

    return data


def _extract_rdap_ip_data(rdap: dict) -> dict:
    """Extract relevant fields from RDAP IP Network response."""
    data: dict = {"type": "ip_network"}
    data["handle"] = rdap.get("handle")
    data["startAddress"] = rdap.get("startAddress")
    data["endAddress"] = rdap.get("endAddress")
    data["ipVersion"] = rdap.get("ipVersion")
    data["name"] = rdap.get("name")
    data["type"] = rdap.get("type")
    data["country"] = rdap.get("country")
    data["status"] = rdap.get("status", [])

    entities = []
    for entity in rdap.get("entities", []):
        vcard = entity.get("vcardArray", [[], []])
        props = {p[0]: p[3] for p in vcard[1] if isinstance(p, list) and len(p) >= 4}
        entities.append({
            "roles": entity.get("roles", []),
            "name": props.get("fn"),
            "email": props.get("email"),
        })
    data["entities"] = entities
    return data


class RDAPLookupTool(BaseTool):
    """
    RDAP Lookup — query domain or IP ownership/registration information.

    Use this tool to look up who owns a domain name (e.g. "example.com") or
    an IP address (e.g. "8.8.8.8"). It relies on the RDAP protocol (the modern
    replacement for WHOIS) and returns registrar, registrant, nameservers,
    status, registration/expiry dates, country, and abuse-contact details.

    Required parameter:
        target (str): A fully-qualified domain name (e.g. "google.com") OR
                      an IPv4/IPv6 address (e.g. "1.1.1.1"). Do NOT pass an
                      empty string or omit this field — the tool will fail.

    Example calls:
        {"target": "cloudflare.com"}
        {"target": "8.8.8.8"}
        {"target": "2606:4700:4700::1111"}

    Permission: ``whois:read``
    Timeout: 10 s · Rate limit: 30/min · Circuit breaker: enabled
    """

    name: ClassVar[str] = "rdap_lookup"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "RDAP registration data lookup for domains, IPs, and ASNs — replaces legacy WHOIS"
    category: ClassVar[str] = "network"
    permissions: ClassVar[list[str]] = ["whois:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 10
    use_circuit_breaker: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "target"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["target"],
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Domain name (e.g. 'example.com') or IP address "
                    "(IPv4 or IPv6) to look up via RDAP. "
                    "This field is required."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}

    state_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "daily_queries_used": {"type": "integer", "default": 0},
        },
    }
    state_defaults: ClassVar[dict] = {"daily_queries_used": 0}
    reset_policy: ClassVar[str] = "daily"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """
        Perform RDAP lookup for domain or IP.

        Params:
            target (str, required): Domain name or IP address.

        Returns:
            Ownership, registration, status information.
        """
        # ── Input validation ──────────────────────────────────────────────
        raw_target = params.get("target", "")
        if not isinstance(raw_target, str):
            return self._failure("INVALID_INPUT", "'target' must be a string (domain or IP)")
        target = raw_target.strip().lower()
        if not target:
            return self._failure("INVALID_INPUT", "'target' parameter is required (domain or IP)")

        is_ip = _is_ip(target)
        if not is_ip and not _DOMAIN_RE.match(target):
            return self._failure("INVALID_INPUT", f"Invalid target: '{target}'. Must be a domain or IP.")

        # ── RDAP query ────────────────────────────────────────────────────
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
            if is_ip:
                rdap_data = await self._lookup_ip(client, target)
            else:
                rdap_data = await self._lookup_domain(client, target)

        if rdap_data is None:
            return self._failure("RDAP_NOT_FOUND", f"No RDAP data found for '{target}'")

        # Update state
        state["daily_queries_used"] = state.get("daily_queries_used", 0) + 1

        return self._success({"target": target, "rdap": rdap_data})

    async def _lookup_domain(
        self,
        client: httpx.AsyncClient,
        domain: str,
    ) -> Optional[dict]:
        """Lookup domain via RDAP, trying known servers first."""
        tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
        base_url = RDAP_FALLBACK_ENDPOINTS.get(tld, "https://rdap.org/domain/")

        # Build RDAP URL: https://rdap.verisign.com/com/v1/domain/google.com
        if base_url.endswith("/"):
            url = f"{base_url}domain/{domain}"
        else:
            url = f"{base_url}/domain/{domain}"

        try:
            response = await client.get(url)
            if response.status_code == 200:
                return _extract_rdap_domain_data(response.json())
            if response.status_code == 404:
                return None
        except httpx.HTTPError:
            pass

        # Fallback: try generic rdap.org
        try:
            response = await client.get(f"https://rdap.org/domain/{domain}")
            if response.status_code == 200:
                return _extract_rdap_domain_data(response.json())
        except httpx.HTTPError:
            pass

        return None

    async def _lookup_ip(
        self,
        client: httpx.AsyncClient,
        ip: str,
    ) -> Optional[dict]:
        """Lookup IP via RDAP (ARIN as primary, RIPE as fallback)."""
        endpoints = [
            f"https://rdap.arin.net/registry/ip/{ip}",
            f"https://rdap.db.ripe.net/ip/{ip}",
            f"https://rdap.lacnic.net/rdap/ip/{ip}",
        ]

        for url in endpoints:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return _extract_rdap_ip_data(response.json())
            except httpx.HTTPError:
                continue

        return None
