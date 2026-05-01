"""csv_soc — SOC-flavoured operations over a stored CSV file.

Provides actions tailored to security analysts that would be awkward to
express in plain SQL:

* ``ip_in_cidr``    — keep / drop rows whose IP column matches one of the
                       supplied CIDR ranges.
* ``ip_classify``   — classify an IP column into ``private``, ``loopback``,
                       ``link_local``, ``multicast``, ``reserved`` or
                       ``public``.
* ``extract_iocs``  — scan one or more text columns for IPs, domains, URLs,
                       email addresses and MD5/SHA1/SHA256 hashes.
* ``geoip_enrich``  — append ``asn`` / ``asn_org`` columns from
                       ``GeoLite2-ASN.mmdb`` (and country fields if a
                       GeoLite2 city/country DB is available).

Permission: ``core:csv_soc``
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import threading
import time
from typing import ClassVar, Optional

import polars as pl

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import load_csv, result_to_payload
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────
_MAX_CIDRS: int = 256
_MAX_RESULT_ROWS: int = 5000
_MAX_INLINE_BYTES: int = 50_000
_MAX_TEXT_COLUMNS: int = 10
_MAX_IOC_PER_TYPE: int = 5000

# ── IOC regexes (re-usable across tools) ───────────────────────────────────
# Domain: at least one dot, alphanumeric labels with hyphens, TLD ≥ 2 chars.
_DOMAIN_RE = re.compile(
    r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,24}\b"
)
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)
_URL_RE = re.compile(
    r"(?:https?|ftp|ftps)://[^\s\"'<>\]\[(){}|\\^`\x00-\x1f]{3,512}",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,24}\b"
)
_MD5_RE = re.compile(r"\b[a-fA-F0-9]{32}\b")
_SHA1_RE = re.compile(r"\b[a-fA-F0-9]{40}\b")
_SHA256_RE = re.compile(r"\b[a-fA-F0-9]{64}\b")

# ── GeoIP DB paths (mirror ip_geolocate tool conventions) ──────────────────
_GEOIP_ASN_PATH: str = os.environ.get(
    "GEOIP_ASN_DB_PATH", "/app/dbs/geoip/GeoLite2-ASN.mmdb"
)
_GEOIP_CITY_PATH: str = os.environ.get(
    "GEOIP_CITY_DB_PATH", "/app/dbs/geoip/GeoLite2-City.mmdb"
)
_GEOIP_COUNTRY_PATH: str = os.environ.get(
    "GEOIP_COUNTRY_DB_PATH", "/app/dbs/geoip/GeoLite2-Country.mmdb"
)

_geoip_lock = threading.Lock()
_asn_reader = None  # type: ignore[var-annotated]
_country_reader = None  # type: ignore[var-annotated]


def _open_reader(path: str):
    """Open a MaxMind reader if the file exists, otherwise return None."""
    if not os.path.isfile(path):
        return None
    try:
        import geoip2.database  # type: ignore[import-untyped]

        return geoip2.database.Reader(path)
    except Exception as exc:
        logger.warning("csv_soc: failed to open %s: %s", path, exc)
        return None


def _get_asn_reader():
    global _asn_reader  # noqa: PLW0603
    if _asn_reader is not None:
        return _asn_reader
    with _geoip_lock:
        if _asn_reader is None:
            _asn_reader = _open_reader(_GEOIP_ASN_PATH)
        return _asn_reader


def _get_country_reader():
    """Return a Reader that exposes ``.country()`` lookups, if available.

    Tries the city DB first (it provides ``.country()`` too) then falls
    back to the country-only DB.
    """
    global _country_reader  # noqa: PLW0603
    if _country_reader is not None:
        return _country_reader
    with _geoip_lock:
        if _country_reader is None:
            _country_reader = _open_reader(_GEOIP_CITY_PATH) or _open_reader(
                _GEOIP_COUNTRY_PATH
            )
        return _country_reader


# ── Helpers ────────────────────────────────────────────────────────────────


def _classify_ip(ip_str: str) -> str:
    """Classify an IP literal.  Returns ``"invalid"`` for unparseable input."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return "invalid"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link_local"
    if addr.is_multicast:
        return "multicast"
    if addr.is_private:
        return "private"
    if addr.is_reserved or addr.is_unspecified:
        return "reserved"
    return "public"


def _build_payload(df: pl.DataFrame) -> dict:
    """Slice + JSON-budget a Polars frame into an inline payload."""
    return result_to_payload(
        df,
        max_rows=_MAX_RESULT_ROWS,
        max_inline_bytes=_MAX_INLINE_BYTES,
    )


# ── ip_in_cidr ─────────────────────────────────────────────────────────────


def _action_ip_in_cidr(
    df: pl.DataFrame, *, column: str, cidrs: list[str], invert: bool
) -> dict:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")

    networks: list[ipaddress._BaseNetwork] = []
    invalid: list[str] = []
    for c in cidrs:
        try:
            networks.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            invalid.append(c)
    if not networks:
        raise ValueError(
            "No valid CIDRs provided." + (f" Invalid: {invalid}" if invalid else "")
        )

    def _matches(value: object) -> bool:
        if value is None:
            return False
        try:
            addr = ipaddress.ip_address(str(value))
        except ValueError:
            return False
        return any(addr in net for net in networks)

    mask = df[column].map_elements(_matches, return_dtype=pl.Boolean)
    if invert:
        mask = ~mask
    filtered = df.filter(mask)
    return {
        "matched_rows": int(filtered.height),
        "invalid_cidrs": invalid,
        "result": _build_payload(filtered),
    }


# ── ip_classify ────────────────────────────────────────────────────────────


def _action_ip_classify(
    df: pl.DataFrame, *, column: str, output_column: str
) -> dict:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")
    classes = df[column].map_elements(
        lambda v: _classify_ip(str(v)) if v is not None else "invalid",
        return_dtype=pl.Utf8,
    )
    enriched = df.with_columns(classes.alias(output_column))
    summary_df = (
        enriched.group_by(output_column)
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
    )
    summary = {row[output_column]: row["count"] for row in summary_df.to_dicts()}
    return {
        "summary": summary,
        "result": _build_payload(enriched),
    }


# ── extract_iocs ───────────────────────────────────────────────────────────


def _scan_text_for_iocs(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {
        "ipv4": set(_IPV4_RE.findall(text)),
        "url": set(_URL_RE.findall(text)),
        "email": set(_EMAIL_RE.findall(text)),
        "md5": set(_MD5_RE.findall(text)),
        "sha1": set(_SHA1_RE.findall(text)),
        "sha256": set(_SHA256_RE.findall(text)),
    }
    # Domains: scan separately and exclude IPv4 / URL host parts already counted.
    domains = set(_DOMAIN_RE.findall(text))
    # Drop pure IPv4 matches and entries that look like hash hex.
    domains = {d for d in domains if not _IPV4_RE.fullmatch(d)}
    found["domain"] = domains
    return found


def _action_extract_iocs(
    df: pl.DataFrame, *, columns: list[str]
) -> dict:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"Column(s) not found: {missing}")
    if len(columns) > _MAX_TEXT_COLUMNS:
        raise ValueError(f"At most {_MAX_TEXT_COLUMNS} columns may be scanned.")

    aggregate: dict[str, set[str]] = {
        k: set() for k in ("ipv4", "domain", "url", "email", "md5", "sha1", "sha256")
    }
    for col in columns:
        series = df[col].cast(pl.Utf8, strict=False)
        for value in series.drop_nulls().to_list():
            if not value:
                continue
            scan = _scan_text_for_iocs(str(value))
            for k, vs in scan.items():
                aggregate[k].update(vs)
                if len(aggregate[k]) > _MAX_IOC_PER_TYPE:
                    # Truncate; keep first _MAX_IOC_PER_TYPE deterministically.
                    aggregate[k] = set(list(aggregate[k])[:_MAX_IOC_PER_TYPE])
    return {
        "scanned_columns": columns,
        "iocs": {k: sorted(v) for k, v in aggregate.items()},
        "counts": {k: len(v) for k, v in aggregate.items()},
    }


# ── geoip_enrich ───────────────────────────────────────────────────────────


def _action_geoip_enrich(df: pl.DataFrame, *, column: str) -> dict:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")

    asn_reader = _get_asn_reader()
    country_reader = _get_country_reader()
    if asn_reader is None and country_reader is None:
        raise ValueError(
            "No GeoIP database available (neither ASN nor Country/City)."
        )

    # Cache lookups per unique IP to amortise over duplicates.
    ip_values = df[column].cast(pl.Utf8, strict=False).to_list()
    unique_ips = sorted({v for v in ip_values if v})

    asn_map: dict[str, Optional[int]] = {}
    asn_org_map: dict[str, Optional[str]] = {}
    cc_map: dict[str, Optional[str]] = {}
    country_map: dict[str, Optional[str]] = {}

    for ip in unique_ips:
        # Skip private / invalid before hitting the readers.
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            asn_map[ip] = None
            asn_org_map[ip] = None
            cc_map[ip] = None
            country_map[ip] = None
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
        ):
            asn_map[ip] = None
            asn_org_map[ip] = None
            cc_map[ip] = None
            country_map[ip] = None
            continue

        if asn_reader is not None:
            try:
                rec = asn_reader.asn(ip)
                asn_map[ip] = rec.autonomous_system_number
                asn_org_map[ip] = rec.autonomous_system_organization
            except Exception:
                asn_map[ip] = None
                asn_org_map[ip] = None
        else:
            asn_map[ip] = None
            asn_org_map[ip] = None

        if country_reader is not None:
            try:
                rec = country_reader.country(ip)
                cc_map[ip] = rec.country.iso_code
                country_map[ip] = rec.country.name
            except Exception:
                cc_map[ip] = None
                country_map[ip] = None
        else:
            cc_map[ip] = None
            country_map[ip] = None

    asn_series = pl.Series(
        "asn", [asn_map.get(v or "") for v in ip_values], dtype=pl.Int64
    )
    asn_org_series = pl.Series(
        "asn_org", [asn_org_map.get(v or "") for v in ip_values], dtype=pl.Utf8
    )
    cc_series = pl.Series(
        "country_code", [cc_map.get(v or "") for v in ip_values], dtype=pl.Utf8
    )
    country_series = pl.Series(
        "country", [country_map.get(v or "") for v in ip_values], dtype=pl.Utf8
    )

    enriched = df.with_columns([asn_series, asn_org_series, cc_series, country_series])
    return {
        "asn_db_available": asn_reader is not None,
        "country_db_available": country_reader is not None,
        "unique_ips_resolved": len(unique_ips),
        "result": _build_payload(enriched),
    }


# ── Tool ───────────────────────────────────────────────────────────────────


class CsvSocTool(BaseTool):
    """SOC-oriented operations over stored CSV files.

    Actions:

    * ``ip_in_cidr``   — filter rows whose IP column matches one of up to
                          256 CIDR ranges. Supports ``invert=true``.
    * ``ip_classify``  — append a column tagging each IP as
                          ``public``/``private``/``loopback``/...
    * ``extract_iocs`` — scan one or more text columns for IPv4, domains,
                          URLs, emails and MD5/SHA1/SHA256 hashes.
    * ``geoip_enrich`` — append ``asn``, ``asn_org`` and (when a GeoLite2
                          country/city DB is available) ``country_code`` /
                          ``country`` columns.

    Permission: ``core:csv_soc``
    """

    name: ClassVar[str] = "csv_soc"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "SOC operations over CSV: CIDR filtering, IP classification, "
        "IOC extraction, and offline GeoIP enrichment."
    )
    category: ClassVar[str] = "data"
    permissions: ClassVar[list[str]] = ["core:csv_soc"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "file_id"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "ip_in_cidr",
                    "ip_classify",
                    "extract_iocs",
                    "geoip_enrich",
                ],
                "description": (
                    "Operation:\n"
                    "- ip_in_cidr: filter rows by CIDR membership.\n"
                    "- ip_classify: tag each IP as public/private/loopback/...\n"
                    "- extract_iocs: scan text columns for IPs/domains/URLs/"
                    "emails/MD5/SHA1/SHA256.\n"
                    "- geoip_enrich: append ASN + (when available) country."
                ),
            },
            "file_id": {
                "type": "string",
                "description": "UUID of the CSV file (GSageFile.id).",
            },
            "column": {
                "type": "string",
                "description": (
                    "IP column name. Required for ip_in_cidr, ip_classify, "
                    "and geoip_enrich."
                ),
            },
            "cidrs": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": _MAX_CIDRS,
                "description": (
                    "CIDR ranges (e.g. '10.0.0.0/8', '2001:db8::/32'). "
                    "Required for ip_in_cidr."
                ),
            },
            "invert": {
                "type": "boolean",
                "description": (
                    "When true, return rows that do NOT match any CIDR. "
                    "Used by ip_in_cidr only. Default false."
                ),
            },
            "output_column": {
                "type": "string",
                "description": (
                    "Name of the column to add for ip_classify "
                    "(default 'ip_class')."
                ),
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": _MAX_TEXT_COLUMNS,
                "description": (
                    "Text columns to scan. Required for extract_iocs."
                ),
            },
            "delimiter": {
                "type": "string",
                "description": (
                    "Override delimiter detection. Allowed values: ',', ';', "
                    "'\\t', '|'. Omit for auto-detect."
                ),
            },
            "encoding": {
                "type": "string",
                "description": "Override encoding detection.",
            },
        },
        "additionalProperties": False,
    }

    audit_field_mapping: ClassVar[dict] = {"target_entities": "file_id"}

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.monotonic()

        action = params.get("action")
        file_id = params.get("file_id")
        if not isinstance(action, str) or action not in {
            "ip_in_cidr", "ip_classify", "extract_iocs", "geoip_enrich"
        }:
            return self._failure("INVALID_INPUT", "'action' is required.")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_INPUT", "'file_id' is required.")

        delimiter = params.get("delimiter")
        encoding = params.get("encoding")
        if delimiter is not None and (
            not isinstance(delimiter, str) or delimiter not in {",", ";", "\t", "|"}
        ):
            return self._failure(
                "INVALID_INPUT",
                "'delimiter' must be one of: ',', ';', '\\t', '|'.",
            )

        try:
            df, file_meta = await load_csv(
                self,
                agent_context,
                file_id,
                delimiter=delimiter if isinstance(delimiter, str) else None,
                encoding=encoding if isinstance(encoding, str) else None,
            )
        except FileNotFoundError as exc:
            return self._failure("FILE_NOT_FOUND", str(exc))
        except ValueError as exc:
            return self._failure("PARSE_ERROR", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("csv_soc: unexpected load failure: %s", exc)
            return self._failure("INTERNAL_ERROR", f"Failed to load CSV: {exc}", retryable=True)

        try:
            if action == "ip_in_cidr":
                column = params.get("column")
                cidrs = params.get("cidrs")
                invert = bool(params.get("invert", False))
                if not isinstance(column, str) or not column:
                    return self._failure("INVALID_INPUT", "'column' is required.")
                if (
                    not isinstance(cidrs, list)
                    or not cidrs
                    or not all(isinstance(c, str) for c in cidrs)
                ):
                    return self._failure(
                        "INVALID_INPUT", "'cidrs' must be a non-empty list of strings."
                    )
                if len(cidrs) > _MAX_CIDRS:
                    return self._failure(
                        "INVALID_INPUT", f"At most {_MAX_CIDRS} CIDRs allowed."
                    )
                view = await asyncio.to_thread(
                    _action_ip_in_cidr,
                    df,
                    column=column,
                    cidrs=cidrs,
                    invert=invert,
                )

            elif action == "ip_classify":
                column = params.get("column")
                output_column = params.get("output_column", "ip_class")
                if not isinstance(column, str) or not column:
                    return self._failure("INVALID_INPUT", "'column' is required.")
                if not isinstance(output_column, str) or not output_column:
                    return self._failure(
                        "INVALID_INPUT", "'output_column' must be a non-empty string."
                    )
                view = await asyncio.to_thread(
                    _action_ip_classify,
                    df,
                    column=column,
                    output_column=output_column,
                )

            elif action == "extract_iocs":
                columns = params.get("columns")
                if (
                    not isinstance(columns, list)
                    or not columns
                    or not all(isinstance(c, str) for c in columns)
                ):
                    return self._failure(
                        "INVALID_INPUT", "'columns' must be a non-empty list of strings."
                    )
                view = await asyncio.to_thread(
                    _action_extract_iocs,
                    df,
                    columns=columns,
                )

            else:  # geoip_enrich
                column = params.get("column")
                if not isinstance(column, str) or not column:
                    return self._failure("INVALID_INPUT", "'column' is required.")
                view = await asyncio.to_thread(
                    _action_geoip_enrich,
                    df,
                    column=column,
                )

        except ValueError as exc:
            return self._failure("INVALID_INPUT", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("csv_soc: action %s failed: %s", action, exc)
            return self._failure("INTERNAL_ERROR", f"Action failed: {exc}")

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": action,
                "file": {
                    "file_id": file_meta.get("file_id"),
                    "filename": file_meta.get("filename"),
                    "rows": file_meta.get("rows"),
                    "columns": file_meta.get("columns"),
                },
                **view,
            },
            execution_time_ms=elapsed,
        )
