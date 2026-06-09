"""gSage AI — DNS Lookup tool (MVP ⭐)."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Max label length RFC 1035 / RFC 2782 (SRV records allow underscore-prefixed labels)
_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9_](?:[a-zA-Z0-9\-_]{0,61}[a-zA-Z0-9_])?\.)+[a-zA-Z]{2,}$"
)
_RECORD_TYPES = {"A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA"}


class DNSLookupTool(BaseTool):
    """
    DNS Lookup — resolve DNS records for domains.

    Resolves: A, AAAA, MX, TXT, NS records.
    Permission: ``dns:read``
    Timeout: 5s per query
    Rate limit: 60 queries/min per org
    Circuit breaker: enabled (external DNS dependency)
    """

    name: ClassVar[str] = "dns_lookup"
    version: ClassVar[str] = "1.1.0"
    summary: ClassVar[str] = "Resolve DNS records (A, AAAA, MX, TXT, NS, CNAME, SOA) for domains and reverse PTR for IPs"
    category: ClassVar[str] = "dns"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["dns:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 10
    use_circuit_breaker: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "domain"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": (
                    "Domain name to resolve (e.g. 'google.com', '_dmarc.example.com') "
                    "or an IPv4/IPv6 address for reverse PTR lookup."
                ),
            },
            "record_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA"]},
                "description": (
                    "DNS record types to fetch. Defaults to [\"A\", \"AAAA\", \"MX\", \"TXT\", \"NS\"]. "
                    "Ignored for reverse PTR lookups."
                ),
                "default": ["A", "AAAA", "MX", "TXT", "NS"],
            },
            "check_blacklists": {
                "type": "boolean",
                "description": (
                    "When true, checks the domain and resolved A/AAAA IPs against DNSBL/RBL blacklists "
                    "using pydnsbl (4 domain providers, 52 IP providers). Increases latency. Default: false."
                ),
                "default": False,
            },
        },
        "required": ["domain"],
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {
        "nameserver": None,  # None = use system default
    }

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
        Resolve DNS records for a domain, or perform a PTR (reverse DNS)
        lookup when an IP address is supplied.

        Params:
            domain (str, required): Domain to resolve (e.g., "google.com")
                or an IPv4/IPv6 address for reverse DNS.
            record_types (list[str], optional): Record types to fetch.
                Defaults to ["A", "AAAA", "MX", "TXT", "NS"].
                Ignored when an IP address is supplied (PTR is used instead).

        Returns:
            dict with per-type results.
        """
        # ── Input validation ──────────────────────────────────────────────
        raw_domain = params.get("domain", "")
        if not isinstance(raw_domain, str):
            return self._failure("INVALID_INPUT", "'domain' must be a string")
        domain = raw_domain.strip().lower().rstrip(".")
        if not domain:
            return self._failure("INVALID_INPUT", "'domain' parameter is required")

        # ── IP address → PTR (reverse DNS) lookup ─────────────────────────
        try:
            addr = ipaddress.ip_address(domain)
            return await self._ptr_lookup(addr, config, state)
        except ValueError:
            pass  # not an IP, continue with domain validation

        if not _DOMAIN_RE.match(domain):
            return self._failure("INVALID_INPUT", f"Invalid domain format: '{domain}'")

        requested_types = params.get("record_types", ["A", "AAAA", "MX", "TXT", "NS"])
        if not isinstance(requested_types, list) or not requested_types:
            requested_types = ["A", "AAAA", "MX", "TXT", "NS"]

        # Sanitize record types
        requested_types = [rt.upper() for rt in requested_types if str(rt).upper() in _RECORD_TYPES]
        if not requested_types:
            requested_types = ["A", "AAAA", "MX", "TXT", "NS"]

        # ── DNS resolution ────────────────────────────────────────────────
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        resolver = dns.asyncresolver.Resolver()
        if config.get("nameserver"):
            resolver.nameservers = [config["nameserver"]]

        records: dict[str, list] = {}
        errors: dict[str, str] = {}

        for rtype in requested_types:
            try:
                answer = await asyncio.wait_for(
                    resolver.resolve(domain, rtype),
                    timeout=self.timeout_seconds,
                )
                records[rtype] = [str(r) for r in answer]
            except dns.resolver.NXDOMAIN:
                errors[rtype] = "NXDOMAIN"
            except dns.resolver.NoAnswer:
                records[rtype] = []  # No records of this type (not an error)
            except dns.exception.Timeout:
                errors[rtype] = "TIMEOUT"
            except dns.exception.DNSException as exc:
                errors[rtype] = str(exc)

        # Update usage counter in state
        state["daily_queries_used"] = state.get("daily_queries_used", 0) + 1

        # ── Build result ──────────────────────────────────────────────────
        data = {
            "domain": domain,
            "records": records,
        }
        if errors:
            data["errors"] = errors

        if errors and not records:
            # All queries failed → full error
            return self._failure(
                code="DNS_ERROR",
                message=f"DNS resolution failed for '{domain}': {errors}",
                retryable=True,
            )

        # ── DNSBL/RBL blacklist check ─────────────────────────────────────
        check_blacklists = params.get("check_blacklists", False)
        if check_blacklists:
            resolved_ips = records.get("A", []) + records.get("AAAA", [])
            bl_data = await self._check_blacklists(domain, resolved_ips)
            data["blacklists"] = bl_data

        if errors:
            # Some succeeded, some failed → partial
            return self._partial(
                data=data,
                code="DNS_PARTIAL",
                message=f"Some record types failed: {list(errors.keys())}",
            )

        return self._success(data)

    async def _check_blacklists(
        self,
        domain: str,
        resolved_ips: list[str],
    ) -> dict:
        """Check domain and IPs against DNSBL/RBL blacklists via pydnsbl.

        pydnsbl's sync .check() calls asyncio.get_event_loop().run_until_complete()
        internally.  When invoked via asyncio.to_thread() the worker thread can
        inherit a reference to the *running* main event loop, causing the
        "this event loop is already running" error.

        Fix: each _run_check() helper creates an isolated event loop for that
        thread so pydnsbl never touches the main loop.
        """
        from pydnsbl import DNSBLDomainChecker, DNSBLIpChecker  # type: ignore[import-untyped]

        results: dict = {}

        def _format_result(r) -> dict:  # type: ignore[no-untyped-def]
            return {
                "blacklisted": r.blacklisted,
                "categories": sorted(str(c) for c in r.categories),
                "detected_by": {str(k): [str(c) for c in v] for k, v in r.detected_by.items()},
                "providers_checked": len(r.providers),
            }

        def _run_check(checker_class, target: str):  # type: ignore[no-untyped-def]
            """Execute a pydnsbl check inside a brand-new, isolated event loop."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                checker = checker_class()
                return checker.check(target)
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        # Domain check (4 providers)
        try:
            domain_result = await asyncio.to_thread(_run_check, DNSBLDomainChecker, domain)
            results["domain"] = _format_result(domain_result)
        except Exception as exc:  # noqa: BLE001
            results["domain"] = {"error": str(exc)}

        # Per-IP check (52 providers) — run in parallel
        if resolved_ips:
            async def _check_ip(ip: str) -> tuple[str, dict]:
                try:
                    r = await asyncio.to_thread(_run_check, DNSBLIpChecker, ip)
                    return ip, _format_result(r)
                except Exception as exc:  # noqa: BLE001
                    return ip, {"error": str(exc)}

            ip_results = await asyncio.gather(*(_check_ip(ip) for ip in resolved_ips))
            results["ips"] = {ip: data for ip, data in ip_results}

        return results

    async def _ptr_lookup(
        self,
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """Perform a PTR (reverse DNS) lookup for the given IP address."""
        import dns.asyncresolver
        import dns.exception
        import dns.resolver
        import dns.reversename

        resolver = dns.asyncresolver.Resolver()
        if config.get("nameserver"):
            resolver.nameservers = [config["nameserver"]]

        ptr_name = dns.reversename.from_address(str(addr))

        state["daily_queries_used"] = state.get("daily_queries_used", 0) + 1

        try:
            answer = await asyncio.wait_for(
                resolver.resolve(ptr_name, "PTR"),
                timeout=self.timeout_seconds,
            )
            hostnames = [str(r).rstrip(".") for r in answer]
            return self._success({
                "ip": str(addr),
                "records": {"PTR": hostnames},
            })
        except dns.resolver.NXDOMAIN:
            return self._success({
                "ip": str(addr),
                "records": {"PTR": []},
                "note": "No PTR record found for this IP",
            })
        except dns.exception.Timeout:
            return self._failure(
                code="DNS_TIMEOUT",
                message=f"PTR lookup timed out for {addr}",
                retryable=True,
            )
        except dns.exception.DNSException as exc:
            return self._failure(
                code="DNS_ERROR",
                message=f"PTR lookup failed for {addr}: {exc}",
                retryable=True,
            )
