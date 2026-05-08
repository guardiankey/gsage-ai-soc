"""gSage AI — Threat Intelligence Lookup tool."""

from __future__ import annotations

import base64
import ipaddress
import logging
import re
from typing import ClassVar, Literal, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult, _tool_session_ctx
from src.shared.cache.decorator import cached
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_CACHE_TTL = 7 * 24 * 3600  # 7 days (threat intel data doesn't change rapidly)

# VirusTotal free-tier API limits
_VT_DAILY_LIMIT = 500

# AbuseIPDB free-tier API limit
_AIPDB_DAILY_LIMIT = 1000

# VirusTotal API endpoints
_VT_BASE = "https://www.virustotal.com/api/v3"

# AbuseIPDB API endpoint
_AIPDB_URL = "https://api.abuseipdb.com/api/v2/check"

# IOC detection patterns
_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_HASH_RE = re.compile(
    r"^[0-9a-fA-F]{32}$"     # MD5
    r"|^[0-9a-fA-F]{40}$"    # SHA1
    r"|^[0-9a-fA-F]{64}$"    # SHA256
)
_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9_](?:[a-zA-Z0-9\-_]{0,61}[a-zA-Z0-9_])?\.)+[a-zA-Z]{2,}$"
)

# AbuseIPDB attack category codes → readable names
_AIPDB_CATEGORIES: dict[int, str] = {
    3: "Fraud Orders", 4: "DDoS Attack", 5: "FTP Brute Force",
    6: "Ping of Death", 7: "Phishing", 8: "Fraud VoIP",
    9: "Open Proxy", 10: "Web Spam", 11: "Email Spam",
    12: "Blog Spam", 13: "VPN IP", 14: "Port Scan",
    15: "Hacking", 16: "SQL Injection", 17: "Spoofing",
    18: "Brute Force", 19: "Bad Web Bot", 20: "Exploited Host",
    21: "Web App Attack", 22: "SSH", 23: "IoT Targeted",
}

# ── IOC detection ──────────────────────────────────────────────────────────


def _detect_ioc_type(ioc: str) -> Optional[Literal["ip", "url", "domain", "hash"]]:
    """Auto-detect IOC type from its format. Returns None if undetermined."""
    stripped = ioc.strip()

    # URL: explicit scheme
    if re.match(r"^https?://|^ftp://", stripped, re.IGNORECASE):
        return "url"

    # IP address (IPv4 or IPv6)
    try:
        ipaddress.ip_address(stripped)
        return "ip"
    except ValueError:
        pass

    # File hash (MD5=32, SHA1=40, SHA256=64 hex chars)
    if _HASH_RE.match(stripped):
        return "hash"

    # Domain (must come after hash check — some hashes look like domains if short)
    if _DOMAIN_RE.match(stripped):
        return "domain"

    return None


def _is_non_routable(ip: str) -> bool:
    """Return True if IP is private, loopback, reserved, or multicast."""
    try:
        addr = ipaddress.ip_address(ip)
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_link_local
            or addr.is_unspecified
        )
    except ValueError:
        return False


# ── Cache / quota helpers ─────────────────────────────────────────────────

_TOOL_NAME: str = "threat_intel_lookup"


class _QuotaExhausted(Exception):
    """Raised when a provider's daily quota is exhausted before the API call.

    Propagates through the ``@cached`` wrapper (not caught) so the caller can
    convert it into a user-facing error without polluting the cache.
    """

    def __init__(self, provider: str, used: int, limit: int) -> None:
        self.provider = provider
        self.used = used
        self.limit = limit
        super().__init__(
            f"Daily {provider} quota exhausted ({used}/{limit} requests). Resets tomorrow."
        )


# ── VirusTotal ─────────────────────────────────────────────────────────────


def _vt_normalize(raw: dict, ioc_type: str) -> dict:
    """Extract relevant fields from a VirusTotal API response."""
    attrs = raw.get("data", {}).get("attributes", {})
    stats = attrs.get("last_analysis_stats", {})
    results = attrs.get("last_analysis_results", {})

    # Collect detection names for malicious/suspicious vendors (capped at 20)
    detections = [
        f"{vendor}: {info.get('result', '?')}"
        for vendor, info in results.items()
        if info.get("category") in ("malicious", "suspicious")
    ][:20]

    normalized: dict = {
        "found": True,
        "malicious": stats.get("malicious", 0),
        "suspicious": stats.get("suspicious", 0),
        "undetected": stats.get("undetected", 0),
        "harmless": stats.get("harmless", 0),
        "analysis_stats": stats,
        "detections": detections,
    }

    if ioc_type == "ip":
        normalized["country"] = attrs.get("country")
        normalized["asn"] = attrs.get("asn")
        normalized["as_owner"] = attrs.get("as_owner")
        normalized["network"] = attrs.get("network")
        normalized["reputation"] = attrs.get("reputation")

    elif ioc_type == "domain":
        normalized["registrar"] = attrs.get("registrar")
        normalized["creation_date"] = attrs.get("creation_date")
        normalized["reputation"] = attrs.get("reputation")
        normalized["categories"] = attrs.get("categories", {})

    elif ioc_type == "hash":
        normalized["meaningful_name"] = attrs.get("meaningful_name")
        normalized["type_description"] = attrs.get("type_description")
        normalized["size"] = attrs.get("size")
        normalized["md5"] = attrs.get("md5")
        normalized["sha1"] = attrs.get("sha1")
        normalized["sha256"] = attrs.get("sha256")

    elif ioc_type == "url":
        normalized["final_url"] = attrs.get("last_final_url")
        normalized["title"] = attrs.get("title")
        normalized["categories"] = attrs.get("categories", {})

    return normalized


@cached(
    ttl=_CACHE_TTL,
    scope="global",
    key_fn=lambda *, ioc, ioc_type, **_: f"vt:{ioc_type}:{ioc.lower()}",
    logical_name=_TOOL_NAME,
)
async def _query_virustotal(
    *,
    ioc: str,
    ioc_type: Literal["ip", "url", "domain", "hash"],
    api_key: str,
    timeout: int,
    state: dict,
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
) -> dict:
    """Call the VirusTotal v3 API and return a normalized result dict.

    Quota is enforced here (before the API call) so that cache HITs never
    consume quota. Raises ``_QuotaExhausted`` if the daily limit is reached.
    """
    vt_daily = state.get("vt_daily_used", 0)
    if vt_daily >= _VT_DAILY_LIMIT:
        raise _QuotaExhausted("VirusTotal", vt_daily, _VT_DAILY_LIMIT)

    if ioc_type == "ip":
        url = f"{_VT_BASE}/ip_addresses/{ioc}"
    elif ioc_type == "domain":
        url = f"{_VT_BASE}/domains/{ioc}"
    elif ioc_type == "hash":
        url = f"{_VT_BASE}/files/{ioc}"
    elif ioc_type == "url":
        # VT expects base64url-encoded URL (no padding) for GET lookup
        encoded = base64.urlsafe_b64encode(ioc.encode()).decode().rstrip("=")
        url = f"{_VT_BASE}/urls/{encoded}"
    else:
        raise ValueError(f"Unsupported IOC type for VirusTotal: {ioc_type}")

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"x-apikey": api_key, "Accept": "application/json"})
        if resp.status_code == 404:
            result = {"found": False, "message": "IOC not found in VirusTotal database"}
        else:
            resp.raise_for_status()
            result = _vt_normalize(resp.json(), ioc_type)

    # Increment quota ONLY on successful API call (after all raises).
    state["vt_daily_used"] = vt_daily + 1
    return result


# ── AbuseIPDB ──────────────────────────────────────────────────────────────


def _aipdb_normalize(raw: dict) -> dict:
    """Extract relevant fields from an AbuseIPDB API response."""
    data = raw.get("data", {})

    # Gather all category codes from individual reports
    raw_cats: list[int] = []
    for report in data.get("reports", []):
        raw_cats.extend(report.get("categories", []))
    unique_cats = sorted({c for c in raw_cats if isinstance(c, int)})
    cat_names = [_AIPDB_CATEGORIES.get(c, f"Category {c}") for c in unique_cats]

    return {
        "found": True,
        "abuse_confidence_score": data.get("abuseConfidenceScore", 0),
        "total_reports": data.get("totalReports", 0),
        "num_distinct_users": data.get("numDistinctUsers", 0),
        "last_reported_at": data.get("lastReportedAt"),
        "country_code": data.get("countryCode"),
        "isp": data.get("isp"),
        "domain": data.get("domain"),
        "usage_type": data.get("usageType"),
        "is_tor": data.get("isTor", False),
        "is_public": data.get("isPublic", True),
        "attack_categories": cat_names,
    }


@cached(
    ttl=_CACHE_TTL,
    scope="global",
    key_fn=lambda *, ip, **_: f"abuseipdb:ip:{ip.lower()}",
    logical_name=_TOOL_NAME,
)
async def _query_abuseipdb(
    *,
    ip: str,
    api_key: str,
    timeout: int,
    state: dict,
    session: AsyncSession,  # noqa: ARG001 — consumed by @cached
    max_age_days: int = 90,
) -> dict:
    """Call the AbuseIPDB v2 API and return a normalized result dict.

    Enforces quota before hitting the API. Raises ``_QuotaExhausted`` when the
    daily limit is reached so cache HITs never consume quota.
    """
    daily = state.get("abuseipdb_daily_used", 0)
    if daily >= _AIPDB_DAILY_LIMIT:
        raise _QuotaExhausted("AbuseIPDB", daily, _AIPDB_DAILY_LIMIT)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            _AIPDB_URL,
            headers={"Key": api_key, "Accept": "application/json"},
            params={"ipAddress": ip, "maxAgeInDays": max_age_days, "verbose": ""},
        )
        if resp.status_code == 404:
            result = {"found": False, "message": "IP not found in AbuseIPDB"}
        else:
            resp.raise_for_status()
            result = _aipdb_normalize(resp.json())

    state["abuseipdb_daily_used"] = daily + 1
    return result


# ── Tool ───────────────────────────────────────────────────────────────────


class ThreatIntelLookupTool(BaseTool):
    """
    Threat Intelligence Lookup — query VirusTotal and AbuseIPDB for any IOC.

    Accepts IP addresses, URLs, domains, and file hashes (MD5/SHA1/SHA256).
    Automatically detects the IOC type unless explicitly specified.

    Results are cached globally for 7 days — repeated lookups for the same IOC
    return instantly from cache without consuming API quota.

    Daily API usage is tracked per org in tool state (reset_policy="daily")
    to avoid exceeding free-tier limits:
        - VirusTotal: 500 requests/day
        - AbuseIPDB: 1 000 requests/day (IP only)

    At least one API key (virustotal_api_key or abuseipdb_api_key) must be
    configured. Missing keys silently skip that provider.

    Permission: ``threat:intel``
    """

    name: ClassVar[str] = "threat_intel_lookup"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Query VirusTotal and AbuseIPDB to assess the threat reputation "
        "of an IP, URL, domain, or file hash (MD5/SHA1/SHA256)"
    )
    category: ClassVar[str] = "threat_intel"
    core_tool: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["threat:intel"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 25
    use_circuit_breaker: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "ioc"}

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["ioc"],
        "properties": {
            "ioc": {
                "type": "string",
                "description": (
                    "Indicator of Compromise to look up. Accepted formats: "
                    "IPv4/IPv6 address, URL (http/https), domain name, "
                    "or file hash (MD5, SHA1, SHA256)."
                ),
            },
            "ioc_type": {
                "type": "string",
                "description": (
                    "Explicit IOC type override. If omitted, type is auto-detected. "
                    "Use 'ip' for addresses, 'url' for full URLs, 'domain' for hostnames, "
                    "'hash' for file hashes."
                ),
                "enum": ["ip", "url", "domain", "hash"],
            },
            "providers": {
                "type": "array",
                "description": (
                    "Limit lookup to specific providers. Defaults to all configured providers. "
                    "AbuseIPDB is skipped automatically for non-IP IOC types."
                ),
                "items": {
                    "type": "string",
                    "enum": ["virustotal", "abuseipdb"],
                },
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "virustotal_api_key": {
            "type": "string",
            "description": "VirusTotal API key (https://www.virustotal.com/gui/my-apikey)",
            "sensitive": True,
        },
        "abuseipdb_api_key": {
            "type": "string",
            "description": "AbuseIPDB API key (https://www.abuseipdb.com/account/api)",
            "sensitive": True,
        },
    }
    config_defaults: ClassVar[dict] = {}

    state_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "vt_daily_used": {
                "type": "integer",
                "description": "VirusTotal API calls used today",
                "default": 0,
            },
            "abuseipdb_daily_used": {
                "type": "integer",
                "description": "AbuseIPDB API calls used today",
                "default": 0,
            },
        },
    }
    state_defaults: ClassVar[dict] = {
        "vt_daily_used": 0,
        "abuseipdb_daily_used": 0,
    }
    # Daily reset is handled by the scheduled reset job
    reset_policy: ClassVar[str] = "daily"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """
        Params:
            ioc (str, required): IP, URL, domain, or file hash to look up.
            ioc_type (str, optional): Explicit type override ('ip','url','domain','hash').
            providers (list[str], optional): Subset of ['virustotal','abuseipdb'].
        """
        import time as _time
        start_ms = int(_time.monotonic() * 1000)

        # ── Validate IOC ──────────────────────────────────────────────────
        raw_ioc = params.get("ioc", "")
        if not isinstance(raw_ioc, str) or not raw_ioc.strip():
            return self._failure("INVALID_INPUT", "'ioc' parameter is required and must be a non-empty string")

        ioc = raw_ioc.strip()
        ioc_type: Optional[str] = params.get("ioc_type")

        if ioc_type is None:
            ioc_type = _detect_ioc_type(ioc)
        if ioc_type is None:
            return self._failure(
                "INVALID_INPUT",
                f"Cannot determine IOC type for: '{ioc}'. "
                "Specify 'ioc_type' explicitly as 'ip', 'url', 'domain', or 'hash'.",
            )

        # Normalize IOC representation
        if ioc_type == "ip":
            try:
                ioc = str(ipaddress.ip_address(ioc))
            except ValueError:
                return self._failure("INVALID_INPUT", f"Invalid IP address: '{ioc}'")
            if _is_non_routable(ioc):
                return self._failure(
                    "INVALID_INPUT",
                    f"'{ioc}' is a private/reserved address — threat intel is not applicable.",
                )
        elif ioc_type in ("domain", "hash"):
            ioc = ioc.lower()

        # ── Resolve active providers ──────────────────────────────────────
        requested_providers: Optional[list] = params.get("providers")
        vt_key: str = (config.get("virustotal_api_key") or "").strip()
        abuseipdb_key: str = (config.get("abuseipdb_api_key") or "").strip()

        want_vt = (requested_providers is None or "virustotal" in requested_providers) and bool(vt_key)
        want_abuseipdb = (
            (requested_providers is None or "abuseipdb" in requested_providers)
            and bool(abuseipdb_key)
            and ioc_type == "ip"  # AbuseIPDB only supports IP addresses
        )

        if not want_vt and not want_abuseipdb:
            if not vt_key and not abuseipdb_key:
                return self._failure(
                    "CONFIG_MISSING",
                    "No threat intel API keys configured. "
                    "Configure 'virustotal_api_key' and/or 'abuseipdb_api_key' in the tool settings.",
                )
            if ioc_type != "ip" and not vt_key:
                return self._failure(
                    "CONFIG_MISSING",
                    f"IOC type '{ioc_type}' requires a VirusTotal API key. "
                    "AbuseIPDB only supports IP address lookups.",
                )

        # ── Get DB session from context var ──────────────────────────────
        session = _tool_session_ctx.get()

        sources: dict = {}
        errors: dict = {}
        from_cache: list[str] = []

        # ── VirusTotal lookup ─────────────────────────────────────────────
        if want_vt:
            if session is None:
                errors["virustotal"] = "DB session unavailable; threat intel cache cannot be used."
            else:
                vt_before = state.get("vt_daily_used", 0)
                try:
                    vt_result = await _query_virustotal(
                        ioc=ioc,
                        ioc_type=ioc_type,  # type: ignore[arg-type]
                        api_key=vt_key,
                        timeout=self.timeout_seconds,
                        state=state,
                        session=session,
                    )
                    sources["virustotal"] = vt_result
                    # Quota is incremented inside the helper only on real API call;
                    # unchanged counter => cache HIT.
                    if state.get("vt_daily_used", 0) == vt_before:
                        from_cache.append("virustotal")
                except _QuotaExhausted as exc:
                    errors["virustotal"] = str(exc)
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code == 401:
                        errors["virustotal"] = "VirusTotal API key is invalid or unauthorized."
                    elif code == 429:
                        errors["virustotal"] = (
                            "VirusTotal API per-minute rate limit exceeded. Retry in 60s."
                        )
                    else:
                        errors["virustotal"] = f"VirusTotal API returned HTTP {code}."
                        logger.warning("VirusTotal error for %s: %s", ioc, exc)
                except httpx.TimeoutException:
                    errors["virustotal"] = "VirusTotal API request timed out."
                except Exception as exc:
                    errors["virustotal"] = f"VirusTotal lookup failed unexpectedly: {exc}"
                    logger.exception("Unexpected VirusTotal error for IOC=%s", ioc)

        # ── AbuseIPDB lookup ──────────────────────────────────────────────
        if want_abuseipdb:
            if session is None:
                errors["abuseipdb"] = "DB session unavailable; threat intel cache cannot be used."
            else:
                aipdb_before = state.get("abuseipdb_daily_used", 0)
                try:
                    aipdb_result = await _query_abuseipdb(
                        ip=ioc,
                        api_key=abuseipdb_key,
                        timeout=self.timeout_seconds,
                        state=state,
                        session=session,
                    )
                    sources["abuseipdb"] = aipdb_result
                    if state.get("abuseipdb_daily_used", 0) == aipdb_before:
                        from_cache.append("abuseipdb")
                except _QuotaExhausted as exc:
                    errors["abuseipdb"] = str(exc)
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code == 401:
                        errors["abuseipdb"] = "AbuseIPDB API key is invalid or unauthorized."
                    elif code == 429:
                        errors["abuseipdb"] = "AbuseIPDB rate limit exceeded."
                    else:
                        errors["abuseipdb"] = f"AbuseIPDB API returned HTTP {code}."
                        logger.warning("AbuseIPDB error for %s: %s", ioc, exc)
                except httpx.TimeoutException:
                    errors["abuseipdb"] = "AbuseIPDB API request timed out."
                except Exception as exc:
                    errors["abuseipdb"] = f"AbuseIPDB lookup failed unexpectedly: {exc}"
                    logger.exception("Unexpected AbuseIPDB error for IOC=%s", ioc)
        elif ioc_type != "ip" and abuseipdb_key:
            # Inform the LLM that AbuseIPDB was skipped (not an IP)
            sources["abuseipdb"] = {
                "skipped": True,
                "reason": "AbuseIPDB only supports IP address lookups.",
            }

        # ── Build result ──────────────────────────────────────────────────
        elapsed = int(_time.monotonic() * 1000) - start_ms

        if not sources and errors:
            return self._failure(
                code="ALL_SOURCES_FAILED",
                message=f"All configured threat intel sources failed: {errors}",
                retryable=True,
                execution_time_ms=elapsed,
            )

        result_data: dict = {
            "ioc": ioc,
            "ioc_type": ioc_type,
            "sources": sources,
        }
        if from_cache:
            result_data["cached_sources"] = from_cache

        if errors:
            result_data["errors"] = errors
            return self._partial(
                data=result_data,
                code="PARTIAL_RESULTS",
                message=f"Some sources failed or hit quota: {', '.join(errors.keys())}",
                execution_time_ms=elapsed,
            )

        return self._success(result_data, execution_time_ms=elapsed)
