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
* ``geoip_enrich``  — append ``asn`` / ``asn_org`` / ``country_code`` /
                       ``country`` columns by reusing the ``ip_geolocate``
                       tool's offline databases (GeoLite2-ASN +
                       IP2Location DB11).  No extra configuration needed.

Permission: ``core:csv_soc``
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from typing import Any, Callable, ClassVar, Optional

import polars as pl

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import invalidate_cache, load_csv
from src.mcp_server.tools.core.csv.csv_shared import (
    _fetch_edited_filenames,
    compute_edited_filename,
    df_to_csv_bytes,
)
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

# ── GeoIP — reuse the ip_geolocate tool's offline DBs (GeoLite2-ASN +
#         IP2Location DB11). Imported lazily so this module still loads
#         when geoip2 / IP2Location are unavailable.
_geoip_lookup: Optional[Callable[[str], dict[str, Any]]] = None
_geoip_import_error: Optional[str] = None

try:
    from src.mcp_server.tools.soc.network.ip_geolocate import (
        _get_asn_reader as _ipgeo_get_asn,  # type: ignore[attr-defined]
        _get_ip2loc as _ipgeo_get_ip2loc,  # type: ignore[attr-defined]
        _lookup_single as _ipgeo_lookup_single,  # type: ignore[attr-defined]
    )
    _geoip_lookup = _ipgeo_lookup_single
except Exception as exc:  # pragma: no cover - import-time guard
    _ipgeo_get_asn = None  # type: ignore[assignment]
    _ipgeo_get_ip2loc = None  # type: ignore[assignment]
    _geoip_import_error = f"{type(exc).__name__}: {exc}"
    logger.warning("csv_soc: ip_geolocate helpers unavailable: %s", _geoip_import_error)


def _geoip_status() -> tuple[bool, bool, Optional[str]]:
    """Return ``(asn_db_available, ip2location_db_available, error)``.

    The lookup is functional whenever at least one of the readers is open.
    """
    if _geoip_lookup is None or _ipgeo_get_asn is None or _ipgeo_get_ip2loc is None:
        return (False, False, _geoip_import_error or "ip_geolocate helpers not importable")
    try:
        asn_ok = _ipgeo_get_asn() is not None
        ip2loc_ok = _ipgeo_get_ip2loc() is not None
    except Exception as exc:  # pragma: no cover - defensive
        return (False, False, f"{type(exc).__name__}: {exc}")
    return (asn_ok, ip2loc_ok, None)


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
        "df": filtered,
        "matched_rows": int(filtered.height),
        "invalid_cidrs": invalid,
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
        "df": enriched,
        "summary": summary,
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
                    aggregate[k] = set(list(aggregate[k])[:_MAX_IOC_PER_TYPE])

    counts = {k: len(v) for k, v in aggregate.items()}
    # Build long-format DataFrame: ioc_type | value
    rows = [
        {"ioc_type": ioc_type, "value": val}
        for ioc_type, vals in aggregate.items()
        for val in sorted(vals)
    ]
    ioc_df = pl.DataFrame(
        rows if rows else [{"ioc_type": "", "value": ""}],
        schema={"ioc_type": pl.Utf8, "value": pl.Utf8},
    )
    if not rows:
        ioc_df = ioc_df.clear()
    return {
        "df": ioc_df,
        "scanned_columns": columns,
        "counts": counts,
        "total_iocs": sum(counts.values()),
    }


# ── geoip_enrich ───────────────────────────────────────────────────────────


def _action_geoip_enrich(df: pl.DataFrame, *, column: str) -> dict:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")

    asn_ok, ip2loc_ok, err = _geoip_status()
    if _geoip_lookup is None or (not asn_ok and not ip2loc_ok):
        raise ValueError(
            "GeoIP enrichment unavailable: "
            + (err or "neither GeoLite2-ASN nor IP2Location databases are loaded.")
            + "  This action reuses the offline databases of the ip_geolocate tool."
        )

    # Cache lookups per unique IP to amortise over duplicates.
    ip_values = df[column].cast(pl.Utf8, strict=False).to_list()
    unique_ips = sorted({v for v in ip_values if v})

    asn_map: dict[str, Optional[int]] = {}
    asn_org_map: dict[str, Optional[str]] = {}
    cc_map: dict[str, Optional[str]] = {}
    country_map: dict[str, Optional[str]] = {}

    for ip in unique_ips:
        try:
            rec = _geoip_lookup(ip)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("csv_soc: ip_geolocate lookup failed for %s: %s", ip, exc)
            asn_map[ip] = None
            asn_org_map[ip] = None
            cc_map[ip] = None
            country_map[ip] = None
            continue
        # ip_geolocate returns null geo fields for invalid/private IPs
        # and skips the network call for them — no extra handling needed.
        asn_map[ip] = rec.get("asn")
        asn_org_map[ip] = rec.get("asn_org")
        cc_map[ip] = rec.get("country_code")
        country_map[ip] = rec.get("country")

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
        "df": enriched,
        "asn_db_available": asn_ok,
        "ip2location_db_available": ip2loc_ok,
        "unique_ips_resolved": len(unique_ips),
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
    rate_limit_per_minute: ClassVar[int] = 300
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
            "create_new": {
                "type": "boolean",
                "description": (
                    "Controls whether the result is saved as a new file or overwrites "
                    "the source in-place.\n"
                    "- true: always create a new file with an incremented suffix "
                    "('_edited.csv', '_edited2.csv', '_edited3.csv', \u2026). "
                    "The system checks existing filenames and picks the first available one.\n"
                    "- false (default): overwrite the source file in-place when its name "
                    "already ends with '_edited*.csv'. If the source is an original file "
                    "(no '_edited*' suffix), a new '_edited.csv' is always created "
                    "regardless \u2014 the original is never overwritten."
                ),
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

        # ── Persist output ────────────────────────────────────────────────
        result_df: pl.DataFrame = view.pop("df")
        summary = {k: v for k, v in view.items()}

        original_filename = file_meta.get("filename") or "output.csv"

        # ── Resolve create_new flag ───────────────────────────────────────
        create_new_param = params.get("create_new")
        create_new: bool = bool(create_new_param) if create_new_param is not None else False

        csv_bytes = await asyncio.to_thread(df_to_csv_bytes, result_df)

        output_file: Optional[dict] = None
        output_filename: str = original_filename
        is_inplace: bool = False
        try:
            from src.shared.database import _get_session_maker  # noqa: PLC0415

            async with _get_session_maker()() as db_session:
                # Query existing _edited* filenames (with advisory lock) to
                # find the next free name and prevent concurrent collisions.
                existing = await _fetch_edited_filenames(
                    org_id=agent_context.org_id,
                    user_id=agent_context.user_id,
                    original_filename=original_filename,
                    session=db_session,
                )
                output_filename, is_inplace = compute_edited_filename(
                    original_filename,
                    force_new=create_new,
                    existing_filenames=existing,
                )

                if is_inplace:
                    output_file = await self._replace_file_content(
                        file_id=str(file_meta.get("file_id", file_id)),
                        data=csv_bytes,
                        agent_context=agent_context,
                        session=db_session,
                    )
                    if output_file is not None:
                        await db_session.commit()
                else:
                    output_file = await self._store_file(
                        data=csv_bytes,
                        filename=output_filename,
                        content_type="text/csv",
                        agent_context=agent_context,
                        session=db_session,
                        description=(
                            f"csv_soc/{action} result from '{original_filename}' "
                            f"({result_df.height} rows, {result_df.width} cols)"
                        ),
                    )
        except Exception as exc:  # pragma: no cover - storage issues are non-fatal
            logger.warning("csv_soc: could not persist output: %s", exc)

        # Invalidate the loader cache so subsequent reads reflect new content.
        org_id_str = str(agent_context.org_id)
        out_file_id = (output_file or {}).get("file_id") or str(file_meta.get("file_id", file_id))
        invalidate_cache(org_id_str, str(file_meta.get("file_id", file_id)))
        if out_file_id != str(file_meta.get("file_id", file_id)):
            invalidate_cache(org_id_str, out_file_id)

        elapsed = int((time.monotonic() - start) * 1000)
        result_data: dict = {
            "action": action,
            "source_file": {
                "file_id": file_meta.get("file_id"),
                "filename": file_meta.get("filename"),
                "rows": file_meta.get("rows"),
                "columns": file_meta.get("columns"),
            },
            "output_file": output_file,
            "is_inplace": is_inplace,
            "output_rows": result_df.height,
            "output_columns": result_df.width,
            **summary,
        }
        if output_file is None:
            result_data["warning"] = (
                "Output could not be saved to storage. "
                "The operation succeeded but the result file is unavailable."
            )
        else:
            hint = (
                "File updated in-place — use the same file_id for further operations."
                if is_inplace
                else (
                    f"New file created: '{output_filename}'. "
                    "Use output_file.file_id for further operations."
                )
            )
            result_data["hint"] = hint
        return self._success(result_data, execution_time_ms=elapsed)
