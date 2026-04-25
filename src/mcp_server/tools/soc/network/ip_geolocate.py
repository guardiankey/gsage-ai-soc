"""gSage AI — IP Geolocation tool.

Queries two local binary databases and returns a combined result:
  - GeoLite2-ASN.mmdb (MaxMind)     → ASN + organisation
  - IP2LOCATION-LITE-DB11.IPV6.BIN  → country, region, city, lat/lon, zip, UTC offset

No external API calls are made.  Both databases are opened once as module-level
singletons and reused across all invocations.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
from typing import ClassVar, Optional, Union

import geoip2.database
import geoip2.errors
import IP2Location  # type: ignore[import-untyped]

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ── Database paths ─────────────────────────────────────────────────────────────
# Override via environment variables when the files live elsewhere.
_GEOIP_ASN_PATH: str = os.environ.get(
    "GEOIP_ASN_DB_PATH", "/app/dbs/geoip/GeoLite2-ASN.mmdb"
)
_IP2LOC_PATH: str = os.environ.get(
    "IP2LOCATION_DB_PATH", "/app/dbs/ip2location/IP2LOCATION-LITE-DB11.IPV6.BIN"
)

_MAX_IPS = 20

# ── Lazy module-level singletons ───────────────────────────────────────────────
# geoip2.database.Reader is documented as thread-safe.
# IP2Location is a C extension; since this process uses asyncio (single-threaded
# event loop) a module-level instance is safe — no true concurrent access occurs.
_asn_reader: Optional[geoip2.database.Reader] = None
_ip2loc_db: Optional[IP2Location.IP2Location] = None
_init_lock = threading.Lock()


def _get_asn_reader() -> Optional[geoip2.database.Reader]:
    global _asn_reader  # noqa: PLW0603
    if _asn_reader is not None:
        return _asn_reader
    with _init_lock:
        if _asn_reader is not None:
            return _asn_reader
        if not os.path.isfile(_GEOIP_ASN_PATH):
            log.warning("GeoLite2-ASN.mmdb not found at %s", _GEOIP_ASN_PATH)
            return None
        try:
            _asn_reader = geoip2.database.Reader(_GEOIP_ASN_PATH)
            log.info("Opened GeoLite2-ASN.mmdb: %s", _GEOIP_ASN_PATH)
        except Exception as exc:
            log.error("Failed to open GeoLite2-ASN.mmdb: %s", exc)
        return _asn_reader


def _get_ip2loc() -> Optional[IP2Location.IP2Location]:
    global _ip2loc_db  # noqa: PLW0603
    if _ip2loc_db is not None:
        return _ip2loc_db
    with _init_lock:
        if _ip2loc_db is not None:
            return _ip2loc_db
        if not os.path.isfile(_IP2LOC_PATH):
            log.warning("IP2Location DB not found at %s", _IP2LOC_PATH)
            return None
        try:
            _ip2loc_db = IP2Location.IP2Location(_IP2LOC_PATH)
            log.info("Opened IP2Location DB: %s", _IP2LOC_PATH)
        except Exception as exc:
            log.error("Failed to open IP2Location DB: %s", exc)
        return _ip2loc_db


# ── IP2Location sentinel value handling ───────────────────────────────────────
# IP2Location returns "-" (or the strings below) when a field has no data.
_IP2LOC_SENTINEL: frozenset[str] = frozenset({
    "-",
    "N/A",
    "",
    "This parameter is unavailable for selected data file",
})


def _ip2loc_str(value: object) -> Optional[str]:
    """Return None if the value is an IP2Location sentinel, otherwise the stripped string."""
    s = str(value).strip() if value is not None else ""
    return None if s in _IP2LOC_SENTINEL else s


def _ip2loc_float(value: object) -> Optional[float]:
    """Return None if value cannot be parsed as float."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ── Per-IP lookup ──────────────────────────────────────────────────────────────

def _lookup_single(ip_str: str) -> dict:
    """Perform both DB lookups for a single IP string and return a combined dict."""
    result: dict = {"ip": ip_str}

    # ── Validate IP format ────────────────────────────────────────────────
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        result["error"] = f"Invalid IP address: {ip_str!r}"
        return result

    # ── Detect private / reserved addresses ───────────────────────────────
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        result["private"] = True
        result["asn"] = None
        result["asn_org"] = None
        result["country_code"] = None
        result["country"] = None
        result["region"] = None
        result["city"] = None
        result["latitude"] = None
        result["longitude"] = None
        result["zip_code"] = None
        result["utc_offset"] = None
        return result

    result["private"] = False

    # ── GeoLite2-ASN lookup ───────────────────────────────────────────────
    asn: Optional[int] = None
    asn_org: Optional[str] = None
    asn_reader = _get_asn_reader()
    if asn_reader is not None:
        try:
            asn_record = asn_reader.asn(ip_str)
            asn = asn_record.autonomous_system_number
            asn_org = asn_record.autonomous_system_organization
        except geoip2.errors.AddressNotFoundError:
            pass
        except Exception as exc:
            log.debug("GeoLite2-ASN lookup failed for %s: %s", ip_str, exc)

    result["asn"] = asn
    result["asn_org"] = asn_org

    # ── IP2Location DB11 lookup ───────────────────────────────────────────
    country_code: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    zip_code: Optional[str] = None
    utc_offset: Optional[str] = None
    ip2loc_db = _get_ip2loc()
    if ip2loc_db is not None:
        try:
            rec = ip2loc_db.get_all(ip_str)
            country_code = _ip2loc_str(getattr(rec, "country_short", None))
            country = _ip2loc_str(getattr(rec, "country_long", None))
            region = _ip2loc_str(getattr(rec, "region", None))
            city = _ip2loc_str(getattr(rec, "city", None))
            latitude = _ip2loc_float(getattr(rec, "latitude", None))
            longitude = _ip2loc_float(getattr(rec, "longitude", None))
            zip_code = _ip2loc_str(getattr(rec, "zipcode", None))
            utc_offset = _ip2loc_str(getattr(rec, "timezone", None))
        except Exception as exc:
            log.debug("IP2Location lookup failed for %s: %s", ip_str, exc)

    result["country_code"] = country_code
    result["country"] = country
    result["region"] = region
    result["city"] = city
    result["latitude"] = latitude
    result["longitude"] = longitude
    result["zip_code"] = zip_code
    result["utc_offset"] = utc_offset

    return result


# ── Tool class ─────────────────────────────────────────────────────────────────


class IpGeolocateTool(BaseTool):
    """IP Geolocation — look up geographic and network data for one or more IP addresses.

    Queries two local offline databases without any external API calls:

    * **GeoLite2-ASN.mmdb** (MaxMind) — Autonomous System Number (ASN) and
      organisation name.
    * **IP2LOCATION-LITE-DB11.IPV6.BIN** — Country, region, city,
      latitude/longitude, ZIP code and UTC offset.

    Results from both sources are merged per IP.  If one database does not
    contain an entry for a given address, the fields from that source will be
    ``null`` — the other source's data is still returned.

    Private, loopback, link-local and reserved addresses are returned with
    ``"private": true`` and all geo fields set to ``null``.

    Supports IPv4 and IPv6.  Accepts a single IP string or a list of up to
    20 IPs per call.

    Permission: ``network:geolocate``
    """

    name: ClassVar[str] = "ip_geolocate"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Look up geographic location (country, city, lat/lon) and ASN for one or more IPs "
        "using offline GeoLite2-ASN + IP2Location databases"
    )
    category: ClassVar[str] = "network"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["network:geolocate"]
    rate_limit_per_minute: ClassVar[int] = 120
    timeout_seconds: ClassVar[int] = 10
    use_circuit_breaker: ClassVar[bool] = False  # local, no external dependency
    requires_approval: ClassVar[bool] = False
    requires_config: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {"ips": "target_entities"}

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["ips"],
        "properties": {
            "ips": {
                "description": (
                    f"One IP address (string) or a list of up to {_MAX_IPS} IP addresses "
                    "(IPv4 or IPv6) to look up.  Example: '8.8.8.8' or ['8.8.8.8', '1.1.1.1']."
                ),
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": _MAX_IPS},
                ],
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
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
        Look up geolocation data for one or more IP addresses.

        Params:
            ips (str | list[str], required): Single IP or list of up to 20 IPs.

        Returns a list of result dicts, one per requested IP, each containing:
            ip (str): The queried address.
            private (bool): True when the address is private/reserved (all geo fields null).
            asn (int | null): Autonomous System Number from GeoLite2-ASN.
            asn_org (str | null): AS organisation name from GeoLite2-ASN.
            country_code (str | null): ISO 3166-1 alpha-2 country code.
            country (str | null): Full country name.
            region (str | null): State / province.
            city (str | null): City name.
            latitude (float | null): Latitude.
            longitude (float | null): Longitude.
            zip_code (str | null): Postal / ZIP code.
            utc_offset (str | null): UTC offset string, e.g. '-07:00'.
            error (str): Present only when the IP string is invalid.
        """
        raw_ips: Union[str, list] = params.get("ips", "")

        # ── Normalise to list ─────────────────────────────────────────────
        if isinstance(raw_ips, str):
            ip_list = [raw_ips.strip()]
        elif isinstance(raw_ips, list):
            ip_list = [str(ip).strip() for ip in raw_ips if str(ip).strip()]
        else:
            return self._failure("INVALID_INPUT", "'ips' must be a string or a list of strings.")

        if not ip_list:
            return self._failure("INVALID_INPUT", "'ips' must not be empty.")

        if len(ip_list) > _MAX_IPS:
            return self._failure(
                "INVALID_INPUT",
                f"Too many IPs requested ({len(ip_list)}). Maximum is {_MAX_IPS} per call.",
            )

        # ── Lookup each IP ────────────────────────────────────────────────
        results = [_lookup_single(ip) for ip in ip_list]

        return self._success({
            "count": len(results),
            "results": results,
            "sources": {
                "asn_db": "GeoLite2-ASN.mmdb" if _get_asn_reader() is not None else None,
                "geo_db": "IP2LOCATION-LITE-DB11.IPV6.BIN" if _get_ip2loc() is not None else None,
            },
        })
