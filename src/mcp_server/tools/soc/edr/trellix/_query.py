"""gSage AI — Trellix EDR query helpers (pure, no I/O state).

Polling, pagination, summarization, payload-builders and small utilities
shared across the Trellix EDR tools.  All helpers are pure (no global
state) except for the polling helpers, which take an open
:class:`TrellixEDRClient` and use :func:`asyncio.sleep`.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from collections import Counter
from typing import Any, Iterable, Literal, Optional

from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRClient, TrellixEDRError

log = logging.getLogger(__name__)

ApiVersion = Literal["v1", "v2"]
HashType = Literal["md5", "sha1", "sha256"]

DEFAULT_POLL_INTERVAL = 15
DEFAULT_MAX_WAIT_SECONDS = 600
DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 5000

# ── Hash detection ──────────────────────────────────────────────────────────


def detect_hash_type(value: str) -> Optional[tuple[HashType, str]]:
    """Detect the hash algorithm from the input length and validate hex.

    Returns ``(algorithm, normalized_lowercase_hex)`` or ``None`` when the
    string is not a valid hex hash of length 32, 40 or 64.
    """
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    try:
        int(s, 16)
    except ValueError:
        return None
    if len(s) == 32:
        return "md5", s
    if len(s) == 40:
        return "sha1", s
    if len(s) == 64:
        return "sha256", s
    return None


# ── Polling ─────────────────────────────────────────────────────────────────


async def wait_for_search(
    client: TrellixEDRClient,
    query_id: str,
    api_version: ApiVersion,
    *,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    max_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> None:
    """Poll until the search completes (HTTP 303) or the deadline is hit.

    Raises :class:`TrellixEDRError` (``code="SEARCH_TIMEOUT"``) on timeout.
    """
    deadline = asyncio.get_event_loop().time() + max_seconds
    while True:
        try:
            done = (
                await client.get_status_v2(query_id)
                if api_version == "v2"
                else await client.get_status_v1(query_id)
            )
        except TrellixEDRError:
            raise
        if done:
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise TrellixEDRError(
                f"Trellix search did not finish within {max_seconds}s (query_id={query_id}).",
                code="SEARCH_TIMEOUT",
            )
        await asyncio.sleep(poll_interval)


# ── Result fetching ─────────────────────────────────────────────────────────


async def fetch_all_results_v2(
    client: TrellixEDRClient,
    query_id: str,
    *,
    max_rows: int = HARD_MAX_ROWS,
) -> tuple[list[dict], dict, bool]:
    """Page through all v2 results.  Returns ``(rows, meta, truncated)``."""
    rows: list[dict] = []
    next_url: Optional[str] = None
    meta: dict = {}
    truncated = False
    while True:
        page, next_url, page_meta = await client.get_results_v2(query_id, next_url=next_url)
        if page_meta and not meta:
            meta = page_meta
        rows.extend(page)
        if len(rows) >= max_rows:
            rows = rows[:max_rows]
            truncated = next_url is not None or len(page) > (max_rows - (len(rows) - len(page)))
            break
        if not next_url:
            break
    total_count = int(meta.get("totalResourceCount", len(rows))) if meta else len(rows)
    total_hosts = int(meta.get("totalHosts", 0)) if meta else 0
    if total_count > len(rows):
        truncated = True
    flat = [_flatten_v2_row(r) for r in rows]
    return flat, {"total_count": total_count, "total_hosts": total_hosts}, truncated


async def fetch_all_results_v1(
    client: TrellixEDRClient,
    query_id: str,
    *,
    max_rows: int = HARD_MAX_ROWS,
    page_size: int = 500,
) -> tuple[list[dict], dict, bool]:
    """Page through all v1 results.  Returns ``(rows, meta, truncated)``."""
    rows: list[dict] = []
    items, meta = await client.get_results_v1(query_id, offset=0, limit=page_size)
    rows.extend(items)
    total_count = int(meta.get("total_count", len(items)))
    total_hosts = int(meta.get("total_hosts", 0))
    truncated = False
    offset = page_size
    while len(rows) < min(total_count, max_rows):
        more, _ = await client.get_results_v1(query_id, offset=offset, limit=page_size)
        if not more:
            break
        rows.extend(more)
        offset += page_size
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True
    if total_count > len(rows):
        truncated = True
    flat = [_flatten_v1_row(r) for r in rows]
    return flat, {"total_count": total_count, "total_hosts": total_hosts}, truncated


def _flatten_v2_row(row: dict) -> dict:
    """Lift ``attributes`` keys to the top level alongside ``id``."""
    out: dict[str, Any] = {}
    rid = row.get("id")
    if rid is not None:
        out["system_id"] = rid
    attrs = row.get("attributes") or {}
    if isinstance(attrs, dict):
        for k, v in attrs.items():
            out[str(k).replace(".", "_")] = v
    return out


def _flatten_v1_row(row: dict) -> dict:
    """Lift ``output`` keys + ``count`` to the top level."""
    out: dict[str, Any] = {}
    output = row.get("output") or {}
    if isinstance(output, dict):
        for k, v in output.items():
            out[str(k).replace(".", "_").replace("|", "_")] = v
    if "count" in row:
        out["count"] = row["count"]
    if "created_at" in row:
        out["created_at"] = row["created_at"]
    return out


# ── Summarization ───────────────────────────────────────────────────────────


# Columns we look at (post-flatten) for default top-N analytics, in priority order.
_DEFAULT_GROUP_KEYS = (
    "HostInfo_hostname",
    "HostInfo_ip_address",
    "Files_sha1",
    "Files_sha256",
    "Files_md5",
    "Files_full_name",
    "Files_status",
    "Processes_name",
    "Processes_sha1",
    "NetworkFlow_remote_ip",
    "NetworkFlow_remote_port",
)


def summarize(
    rows: list[dict],
    *,
    group_by: Optional[Iterable[str]] = None,
    top_n: int = 10,
    sample_size: int = 20,
) -> dict:
    """Build a generic top-N + distinct-counts summary over flattened rows."""
    if not rows:
        return {"row_count": 0, "distinct": {}, "top": {}, "sample": []}

    keys: list[str]
    if group_by:
        keys = [str(k) for k in group_by if k]
    else:
        present = set().union(*(r.keys() for r in rows))
        keys = [k for k in _DEFAULT_GROUP_KEYS if k in present][:8]

    distinct: dict[str, int] = {}
    top: dict[str, list[dict]] = {}
    for k in keys:
        values = [r.get(k) for r in rows if r.get(k) not in (None, "")]
        distinct[k] = len({_hashable(v) for v in values})
        counter: Counter[Any] = Counter(_hashable(v) for v in values)
        top[k] = [
            {"value": val, "count": cnt}
            for val, cnt in counter.most_common(top_n)
        ]

    return {
        "row_count": len(rows),
        "distinct": distinct,
        "top": top,
        "sample": rows[:sample_size],
    }


def _hashable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


# ── Exports ─────────────────────────────────────────────────────────────────


def export_to_csv(rows: list[dict]) -> bytes:
    """Encode rows as UTF-8 CSV.  Columns are the union of all keys, sorted."""
    if not rows:
        return b""
    columns: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: _csv_value(r.get(k)) for k in columns})
    return buf.getvalue().encode("utf-8")


def _csv_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


def export_to_json(rows: list[dict]) -> bytes:
    return json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")


# ── v1 payload builders ─────────────────────────────────────────────────────


def build_files_payload(
    *,
    file_name: Optional[str] = None,
    hash_type: Optional[HashType] = None,
    hash_value: Optional[str] = None,
    hostname_contains: Optional[str] = None,
    hostname_equals: Optional[str] = None,
) -> dict:
    """Build a v1 search payload for the Files projection.

    At least one of the criteria must be provided (validation is the caller's
    responsibility).
    """
    conditions: list[dict] = []
    if file_name:
        conditions.append(
            {"name": "Files", "output": "full_name", "op": "CONTAINS", "value": file_name}
        )
    if hash_type and hash_value:
        conditions.append(
            {"name": "Files", "output": hash_type, "op": "EQUALS", "value": hash_value}
        )
    if hostname_contains:
        conditions.append(
            {"name": "HostInfo", "output": "hostname", "op": "CONTAINS", "value": hostname_contains}
        )
    if hostname_equals:
        conditions.append(
            {"name": "HostInfo", "output": "hostname", "op": "EQUALS", "value": hostname_equals}
        )

    return {
        "projections": [
            {"name": "HostInfo", "outputs": ["hostname", "ip_address"]},
            {
                "name": "Files",
                "outputs": ["name", "sha1", "sha256", "md5", "status", "full_name"],
            },
        ],
        "condition": {"or": [{"and": conditions}]},
    }


def build_network_payload(
    *,
    remote_ip: Optional[str] = None,
    remote_port: Optional[int] = None,
    process_name: Optional[str] = None,
    hostname_contains: Optional[str] = None,
    direction: Optional[str] = None,
) -> dict:
    """Build a v1 search payload for the NetworkFlow projection."""
    conditions: list[dict] = []
    if remote_ip:
        conditions.append(
            {"name": "NetworkFlow", "output": "remote_ip", "op": "EQUALS", "value": remote_ip}
        )
    if remote_port is not None:
        conditions.append(
            {
                "name": "NetworkFlow",
                "output": "remote_port",
                "op": "EQUALS",
                "value": int(remote_port),
            }
        )
    if process_name:
        conditions.append(
            {"name": "Processes", "output": "name", "op": "CONTAINS", "value": process_name}
        )
    if hostname_contains:
        conditions.append(
            {"name": "HostInfo", "output": "hostname", "op": "CONTAINS", "value": hostname_contains}
        )
    if direction in ("in", "out"):
        conditions.append(
            {"name": "NetworkFlow", "output": "direction", "op": "EQUALS", "value": direction}
        )

    return {
        "projections": [
            {"name": "HostInfo", "outputs": ["hostname", "ip_address"]},
            {
                "name": "NetworkFlow",
                "outputs": ["remote_ip", "remote_port", "local_port", "direction", "protocol"],
            },
            {"name": "Processes", "outputs": ["name", "command_line", "sha1"]},
        ],
        "condition": {"or": [{"and": conditions}]},
    }


def build_host_locator_payload(
    *,
    hostname: Optional[str] = None,
    ip_address: Optional[str] = None,
    exact: bool = True,
) -> dict:
    """Tiny v1 payload to locate a host by hostname/IP and capture its system_id."""
    conditions: list[dict] = []
    if hostname:
        conditions.append(
            {
                "name": "HostInfo",
                "output": "hostname",
                "op": "EQUALS" if exact else "CONTAINS",
                "value": hostname,
            }
        )
    if ip_address:
        conditions.append(
            {
                "name": "HostInfo",
                "output": "ip_address",
                "op": "EQUALS" if exact else "CONTAINS",
                "value": ip_address,
            }
        )

    return {
        "projections": [
            {
                "name": "HostInfo",
                "outputs": ["hostname", "ip_address", "os_name", "os_version"],
            },
        ],
        "condition": {"or": [{"and": conditions}]},
    }


def build_host_locator_query(
    *,
    hostname: Optional[str] = None,
    ip_address: Optional[str] = None,
    exact: bool = True,
) -> str:
    """Build a v2 SQL-like locator query.

    v2 results expose one row per host (with the host's ``system_id`` as the
    row ``id``), which is what we need to feed the v1 remediation API.
    """
    op = "equals" if exact else "contains"
    if hostname:
        return f'HostInfo hostname, ip_address, os_name WHERE HostInfo hostname {op} "{_escape(hostname)}"'
    if ip_address:
        return f'HostInfo hostname, ip_address, os_name WHERE HostInfo ip_address {op} "{_escape(ip_address)}"'
    raise TrellixEDRError("locator query requires hostname or ip_address.", code="INVALID_INPUT")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Pipeline helper ─────────────────────────────────────────────────────────


def clamp_max_rows(value: Optional[int]) -> int:
    if value is None:
        return DEFAULT_MAX_ROWS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROWS
    if n <= 0:
        return DEFAULT_MAX_ROWS
    return min(n, HARD_MAX_ROWS)


# ── Shared tool config schema ───────────────────────────────────────────────

TRELLIX_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["client_id", "client_secret", "x_api_key"],
    "properties": {
        "client_id": {
            "type": "string",
            "description": (
                "Trellix OAuth2 client_id (issued in the Trellix console under "
                "Settings → API Access)."
            ),
        },
        "client_secret": {
            "type": "string",
            "description": "Trellix OAuth2 client_secret.",
            "format": "password",
            "sensitive": True,
        },
        "x_api_key": {
            "type": "string",
            "description": (
                "Trellix x-api-key header value (issued together with the OAuth2 "
                "credentials)."
            ),
            "format": "password",
            "sensitive": True,
        },
        "region": {
            "type": "string",
            "description": (
                "Trellix SOC region tag, used to build the v1 (Active Response) "
                "host: api.soc.<region>.trellix.com.  Default: 'us-east-1'."
            ),
        },
        "base_url_v2": {
            "type": "string",
            "description": (
                "Override the v2 base URL.  Default: https://api.manage.trellix.com."
            ),
        },
        "token_url": {
            "type": "string",
            "description": (
                "Override the OAuth2 token endpoint.  "
                "Default: https://auth.trellix.com/auth/realms/IAM/protocol/openid-connect/token."
            ),
        },
        "verify_tls": {
            "type": "boolean",
            "description": "Verify TLS certificates (default: true).",
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "HTTP request timeout in seconds (default: 60).",
        },
    },
    "additionalProperties": False,
}


TRELLIX_CONFIG_DEFAULTS: dict = {
    "region": "us-east-1",
    "base_url_v2": "https://api.manage.trellix.com",
    "token_url": "https://auth.trellix.com/auth/realms/IAM/protocol/openid-connect/token",
    "verify_tls": True,
    "timeout": 60,
}


def build_client(config: dict) -> TrellixEDRClient:
    """Instantiate a :class:`TrellixEDRClient` from a tool config dict."""
    return TrellixEDRClient(
        client_id=str(config.get("client_id") or ""),
        client_secret=str(config.get("client_secret") or ""),
        x_api_key=str(config.get("x_api_key") or ""),
        region=str(config.get("region") or "us-east-1"),
        base_url_v2=str(config.get("base_url_v2") or "") or None,
        token_url=str(config.get("token_url") or "") or None,
        verify_tls=bool(config.get("verify_tls", True)),
        timeout=float(config.get("timeout", 60)),
    )


# ── Search pipeline used by every search tool ───────────────────────────────


async def run_search_pipeline(
    client: TrellixEDRClient,
    *,
    api_version: ApiVersion,
    query: Optional[str] = None,
    payload: Optional[dict] = None,
    max_rows: int,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> tuple[str, list[dict], dict, bool]:
    """Start → wait → fetch_all.  Returns ``(query_id, rows, meta, truncated)``."""
    if api_version == "v2":
        if not query:
            raise TrellixEDRError("v2 search requires a query string.", code="INVALID_INPUT")
        query_id = await client.start_search_v2(query)
    else:
        if not payload:
            raise TrellixEDRError("v1 search requires a payload object.", code="INVALID_INPUT")
        query_id = await client.start_search_v1(payload)

    await wait_for_search(
        client,
        query_id,
        api_version,
        poll_interval=poll_interval,
        max_seconds=max_wait_seconds,
    )

    if api_version == "v2":
        rows, meta, truncated = await fetch_all_results_v2(client, query_id, max_rows=max_rows)
    else:
        rows, meta, truncated = await fetch_all_results_v1(client, query_id, max_rows=max_rows)

    return query_id, rows, meta, truncated
