"""gSage AI — Trellix EDR query helpers (pure, no I/O state).

Polling, pagination, summarization, payload-builders and small utilities
shared across the Trellix EDR tools.  All helpers are pure (no global
state) except for the polling helpers, which take an open
:class:`TrellixEDRClient` and use :func:`asyncio.sleep`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Literal, Optional

from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    export_to_csv,
    export_to_json,
)
from src.mcp_server.tools.result_export import summarize as _generic_summarize
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRClient, TrellixEDRError

log = logging.getLogger(__name__)

ApiVersion = Literal["v1", "v2"]
HashType = Literal["md5", "sha1", "sha256"]

DEFAULT_POLL_INTERVAL = 15
DEFAULT_MAX_WAIT_SECONDS = 600
DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 5000

# HTTP status codes that indicate a retryable / transient error from Trellix.
# 403 is included because Trellix sometimes returns it with an
# "Internal Server Error" body on the queue-jobs status endpoint — this is
# clearly a server-side bug, not an authorisation rejection.
TRELLIX_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({403, 429, 500, 502, 503, 504})


def is_retryable_error(exc: "TrellixEDRError") -> bool:
    """Return True when the error is safe to retry (transient server issue)."""
    if exc.status_code in TRELLIX_RETRYABLE_STATUS_CODES:
        return True
    return False


# Re-export AGENT_PREVIEW_ROWS / serialisers from the shared helper so existing
# imports (Q.AGENT_PREVIEW_ROWS, Q.export_to_csv, Q.export_to_json) keep
# working. New tools should import directly from
# ``src.mcp_server.tools.result_export``.
__all_re_exports__ = ("AGENT_PREVIEW_ROWS", "export_to_csv", "export_to_json")

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

    Transient HTTP errors from the status endpoint (5xx, 429, and 403 with
    an "Internal Server Error" body — a known Trellix quirk when the
    queue-jobs endpoint temporarily fails for v1 search IDs) are treated as
    "not done yet" and the poll loop continues.  A fatal error is raised only
    when the same transient status persists for ``_POLL_MAX_TRANSIENT``
    consecutive cycles.
    """
    # Status codes that are transient on the queue-jobs endpoint.
    # 403 is included because Trellix sometimes returns 403 with an
    # "Internal Server Error" body (not an authorisation error).
    _POLL_TRANSIENT_CODES = frozenset({403, 429, 500, 502, 503, 504})
    _POLL_MAX_TRANSIENT = 5  # abort after this many consecutive transient errors

    consecutive_transient = 0
    deadline = asyncio.get_event_loop().time() + max_seconds
    while True:
        try:
            done = (
                await client.get_status_v2(query_id)
                if api_version == "v2"
                else await client.get_status_v1(query_id)
            )
            consecutive_transient = 0  # reset on success
        except TrellixEDRError as exc:
            if exc.status_code in _POLL_TRANSIENT_CODES:
                consecutive_transient += 1
                log.warning(
                    "trellix poll: transient HTTP %s on queue-jobs "
                    "(attempt %d/%d, query_id=%s) — continuing poll",
                    exc.status_code,
                    consecutive_transient,
                    _POLL_MAX_TRANSIENT,
                    query_id,
                )
                if consecutive_transient >= _POLL_MAX_TRANSIENT:
                    raise TrellixEDRError(
                        f"Trellix status endpoint returned HTTP {exc.status_code} "
                        f"{_POLL_MAX_TRANSIENT} times in a row (query_id={query_id}): {exc}",
                        status_code=exc.status_code,
                        code=exc.code,
                    ) from exc
                done = False
            else:
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
    "NetworkFlow_src_ip",
    "NetworkFlow_dst_ip",
    "NetworkFlow_dst_port",
    "NetworkFlow_process",
)


def summarize(
    rows: list[dict],
    *,
    group_by: Optional[Iterable[str]] = None,
    top_n: int = 10,
    sample_size: int = 20,
) -> dict:
    """Top-N + distinct-counts summary, defaulting to Trellix-aware columns.

    Thin wrapper around :func:`src.mcp_server.tools.result_export.summarize`
    that injects :data:`_DEFAULT_GROUP_KEYS` (the Trellix-flattened column
    names) as the heuristic when no explicit ``group_by`` is provided.
    """
    return _generic_summarize(
        rows,
        group_by=group_by,
        top_n=top_n,
        sample_size=sample_size,
        default_keys=_DEFAULT_GROUP_KEYS,
    )


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
                "outputs": ["name", "sha1", "sha256", "md5", "status", "full_name", "created_at", "create_user_name"],
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
    """Build a v1 search payload for the NetworkFlow projection.

    The Trellix NetworkFlow collector exposes ``src_ip``/``dst_ip`` and
    ``src_port``/``dst_port`` (there is no ``remote_ip``/``remote_port``
    field). To keep the public API ergonomic, ``remote_ip`` and
    ``remote_port`` are translated into an OR-block that matches either the
    source or the destination, so callers don't need to know the flow
    direction up-front. Process attribution lives inside NetworkFlow itself
    (``process`` and ``process_id``), so we filter and project them there.
    """
    # Conjunctive conditions (AND-block).
    and_conditions: list[dict] = []
    if process_name:
        and_conditions.append(
            {"name": "NetworkFlow", "output": "process", "op": "CONTAINS", "value": process_name}
        )
    if hostname_contains:
        and_conditions.append(
            {"name": "HostInfo", "output": "hostname", "op": "CONTAINS", "value": hostname_contains}
        )
    if direction:
        and_conditions.append(
            {"name": "NetworkFlow", "output": "direction", "op": "EQUALS", "value": direction}
        )

    # ``remote_ip`` / ``remote_port`` match either side of the flow. We build
    # one OR-block per match and combine them with the AND-conditions above:
    #   (src_ip=X OR dst_ip=X) AND (src_port=Y OR dst_port=Y) AND <rest>
    # In the v1 condition tree this is expressed as an outer OR of
    # AND-blocks, where each AND-block carries one of the side combinations.
    side_combinations: list[list[dict]] = [[]]
    if remote_ip:
        side_combinations = [
            block + [{"name": "NetworkFlow", "output": "src_ip", "op": "EQUALS", "value": remote_ip}]
            for block in side_combinations
        ] + [
            block + [{"name": "NetworkFlow", "output": "dst_ip", "op": "EQUALS", "value": remote_ip}]
            for block in side_combinations
        ]
    if remote_port is not None:
        port_value = int(remote_port)
        side_combinations = [
            block + [{"name": "NetworkFlow", "output": "src_port", "op": "EQUALS", "value": port_value}]
            for block in side_combinations
        ] + [
            block + [{"name": "NetworkFlow", "output": "dst_port", "op": "EQUALS", "value": port_value}]
            for block in side_combinations
        ]

    or_blocks = [
        {"and": and_conditions + extras}
        for extras in side_combinations
    ]

    return {
        "projections": [
            {"name": "HostInfo", "outputs": ["hostname", "ip_address"]},
            {
                "name": "NetworkFlow",
                "outputs": [
                    "src_ip",
                    "src_port",
                    "dst_ip",
                    "dst_port",
                    "proto",
                    "direction",
                    "status",
                    "time",
                    "process",
                    "process_id",
                    "user",
                    "sha256",
                ],
            },
        ],
        "condition": {"or": or_blocks},
    }


# Reputation buckets considered "suspicious" (lower scores in Trellix scale).
# See Processes.process_reputation field documentation.
SUSPICIOUS_REPUTATIONS: tuple[str, ...] = (
    "Known Malicious",
    "Most Likely Malicious",
    "Might Be Malicious",
)

PROCESS_EXECUTION_MODES: tuple[str, ...] = (
    "Interactive",
    "Unknown",
    "File",
    "Commandline",
    "Mar_child",
)

ProcessCollector = Literal["Processes", "ProcessHistory"]


def build_processes_payload(
    *,
    collector: ProcessCollector = "Processes",
    process_name_contains: Optional[str] = None,
    process_name_equals: Optional[str] = None,
    cmdline_contains: Optional[str] = None,
    parent_cmdline_contains: Optional[str] = None,
    parent_name_equals: Optional[str] = None,
    parent_name_not_equals: Optional[str] = None,
    user_equals: Optional[str] = None,
    hash_type: Optional[HashType] = None,
    hash_value: Optional[str] = None,
    imagepath_contains: Optional[str] = None,
    execution_mode: Optional[str] = None,
    suspicious_reputation_only: bool = False,
    started_after: Optional[str] = None,
    started_before: Optional[str] = None,
    hostname_contains: Optional[str] = None,
    hostname_equals: Optional[str] = None,
    include_host_info: bool = False,
    include_powershell_content: bool = False,
) -> dict:
    """Build a v1 search payload for the Processes / ProcessHistory collector.

    When ``include_host_info`` is False (default), the projection contains
    only the process collector — Trellix returns one aggregated row per
    distinct process tuple, ideal for fleet-wide hunting (e.g. "list every
    SHA1 of running processes in the org").  When True, ``HostInfo`` is
    added to the projection and rows are duplicated per host, which can
    exceed API limits if no narrow filter is applied.

    All filters are optional — an empty payload is allowed and returns the
    full inventory aggregated by process attributes (intended for hunting).

    Note: ``parent_cmdline`` is only available on the ``Processes`` collector,
    not on ``ProcessHistory``.  Passing ``parent_cmdline_contains`` with
    ``collector='ProcessHistory'`` is the caller's responsibility.
    """
    conditions: list[dict] = []

    def _add(output: str, op: str, value: Any) -> None:
        conditions.append({"name": collector, "output": output, "op": op, "value": value})

    if process_name_contains:
        _add("name", "CONTAINS", process_name_contains)
    if process_name_equals:
        _add("name", "EQUALS", process_name_equals)
    if cmdline_contains:
        _add("cmdline", "CONTAINS", cmdline_contains)
    if parent_cmdline_contains and collector == "Processes":
        _add("parent_cmdline", "CONTAINS", parent_cmdline_contains)
    if parent_name_equals:
        _add("parentname", "EQUALS", parent_name_equals)
    if parent_name_not_equals:
        _add("parentname", "NOT_EQUALS", parent_name_not_equals)
    if user_equals:
        _add("user", "EQUALS", user_equals)
    if hash_type and hash_value:
        _add(hash_type, "EQUALS", hash_value)
    if imagepath_contains:
        _add("imagepath", "CONTAINS", imagepath_contains)
    if execution_mode:
        _add("execution_mode", "EQUALS", execution_mode)
    if started_after:
        # Trellix v1 supports GREATER_EQUAL on timestamp fields (ISO 8601).
        _add("started_at", "GREATER_EQUAL", started_after)
    if started_before:
        _add("started_at", "LESS_EQUAL", started_before)
    if suspicious_reputation_only:
        # Reputation is a single-valued string; OR it across the suspicious
        # buckets within the same AND group is not expressible — the caller
        # gets an OR-block of AND-conditions instead (see condition assembly
        # below).  We attach a marker handled by the assembler.
        pass

    if hostname_contains:
        conditions.append(
            {"name": "HostInfo", "output": "hostname", "op": "CONTAINS", "value": hostname_contains}
        )
    if hostname_equals:
        conditions.append(
            {"name": "HostInfo", "output": "hostname", "op": "EQUALS", "value": hostname_equals}
        )

    base_outputs = [
        "name",
        "id",
        "cmdline",
        "parentname",
        "parentid",
        "parentimagepath",
        "imagepath",
        "user",
        "user_id",
        "md5",
        "sha1",
        "sha256",
        # ``file_reputation`` is valid for both collectors.
        # ``process_reputation`` is only valid for the live Processes collector
        # — the ProcessHistory collector rejects it with AR-806
        # ("Output process_reputation is not valid for collector ProcessHistory")
        # even though the field appears in the official documentation table.
        "file_reputation",
        "execution_mode",
        "started_at",
        "size",
        "threadcount",
    ]
    if collector == "Processes":
        # ``process_reputation`` and ``parent_cmdline`` are only valid for the
        # live Processes collector.
        base_outputs.extend(["process_reputation", "parent_cmdline"])
    if collector == "ProcessHistory":
        # ``finished_at`` and ``status`` are only emitted by ProcessHistory.
        base_outputs.extend(["finished_at", "status"])
    if include_powershell_content:
        base_outputs.extend(["content", "content_size", "content_file"])

    projections: list[dict] = []
    if include_host_info:
        projections.append(
            {"name": "HostInfo", "outputs": ["hostname", "ip_address", "os_name"]}
        )
    projections.append({"name": collector, "outputs": base_outputs})

    # Build the top-level condition.  When suspicious_reputation_only is set,
    # we OR together one AND-block per reputation bucket so each block has
    # the same (AND) filters plus a different reputation EQUALS condition.
    # ProcessHistory does not expose ``process_reputation`` — use
    # ``file_reputation`` instead (same value scale, valid for both collectors).
    if suspicious_reputation_only:
        rep_output = "process_reputation" if collector == "Processes" else "file_reputation"
        and_blocks: list[dict] = []
        for rep in SUSPICIOUS_REPUTATIONS:
            block = list(conditions) + [
                {
                    "name": collector,
                    "output": rep_output,
                    "op": "EQUALS",
                    "value": rep,
                }
            ]
            and_blocks.append({"and": block})
        condition: dict = {"or": and_blocks}
    else:
        condition = {"or": [{"and": conditions}]} if conditions else {"or": [{"and": []}]}

    return {"projections": projections, "condition": condition}


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


# ── v1 payload validation ───────────────────────────────────────────────────


def _check_condition_tree(condition: Any, *, path: str) -> Optional[str]:
    """Recursively walk a v1 condition tree looking for null/missing outputs.

    Returns a human-readable error string on the first problem found, or
    ``None`` when the tree is structurally valid.
    """
    if isinstance(condition, list):
        for i, item in enumerate(condition):
            err = _check_condition_tree(item, path=f"{path}[{i}]")
            if err:
                return err
        return None
    if not isinstance(condition, dict):
        return None
    # Leaf condition: identified by the presence of "op".
    if "op" in condition:
        collector = condition.get("name") or "unknown"
        output = condition.get("output")
        if output is None:
            return (
                f"{path}: condition for collector '{collector}' has a null/missing "
                "'output' field — specify the exact field name "
                "(e.g. 'displayname', 'version', 'hostname')"
            )
        return None
    # Branch node: {"and": [...]} or {"or": [...]}.
    for key in ("and", "or"):
        if key in condition:
            err = _check_condition_tree(condition[key], path=f"{path}.{key}")
            if err:
                return err
    return None


def validate_v1_payload(payload: dict) -> Optional[str]:
    """Validate a v1 Active Response payload before submitting to the API.

    Returns a descriptive error string when the payload is structurally
    invalid, or ``None`` when it looks correct.  Designed to catch common
    LLM-generated mistakes (null output fields, missing projections) before
    they reach the Trellix API and produce cryptic AR-806 errors.
    """
    if not isinstance(payload, dict):
        return "payload must be a JSON object"

    projections = payload.get("projections")
    if not projections or not isinstance(projections, list):
        return "payload must have a non-empty 'projections' list"

    for i, proj in enumerate(projections):
        if not isinstance(proj, dict):
            return f"projections[{i}] must be an object"
        if not proj.get("name"):
            return f"projections[{i}] is missing 'name'"
        outputs = proj.get("outputs")
        if not outputs or not isinstance(outputs, list):
            return (
                f"projections[{i}] ({proj.get('name')!r}) must have a "
                "non-empty 'outputs' list"
            )
        for j, out in enumerate(outputs):
            if out is None:
                return (
                    f"projections[{i}].outputs[{j}] is null — "
                    "provide the field name (e.g. 'displayname')"
                )

    condition = payload.get("condition")
    if condition is None:
        return "payload is missing 'condition'"

    return _check_condition_tree(condition, path="condition")


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
        return (
            'HostInfo hostname, ip_address, platform, os, connection_status '
            f'WHERE HostInfo hostname {op} "{_escape(hostname)}"'
        )
    if ip_address:
        return (
            'HostInfo hostname, ip_address, platform, os, connection_status '
            f'WHERE HostInfo ip_address {op} "{_escape(ip_address)}"'
        )
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
    """Start → wait → fetch_all.  Returns ``(query_id, rows, meta, truncated)``.

    For v2 searches, the Trellix API occasionally returns HTTP 303 on the
    queue-jobs status endpoint (indicating the search is "done") before the
    results are actually queryable at the /results URL.  To handle this
    timing window, a 404 on the first fetch attempt is retried up to 3 times
    with a 10-second delay before the error is propagated.

    For v1 searches, the payload is validated locally before submission so
    that common mistakes (e.g. ``"output": null``) surface as a clear
    ``INVALID_INPUT`` error instead of the cryptic AR-806 from the API.
    """
    if api_version == "v2":
        if not query:
            raise TrellixEDRError("v2 search requires a query string.", code="INVALID_INPUT")
        query_id = await client.start_search_v2(query)
    else:
        if not payload:
            raise TrellixEDRError("v1 search requires a payload object.", code="INVALID_INPUT")
        validation_error = validate_v1_payload(payload)
        if validation_error:
            raise TrellixEDRError(
                f"Invalid v1 payload: {validation_error}",
                code="INVALID_INPUT",
            )
        query_id = await client.start_search_v1(payload)

    await wait_for_search(
        client,
        query_id,
        api_version,
        poll_interval=poll_interval,
        max_seconds=max_wait_seconds,
    )

    if api_version == "v2":
        # Retry on 404: the /results endpoint may not be ready immediately
        # after the queue-jobs 303, especially under load.
        _v2_result_retries = 3
        _v2_result_retry_delay = 10
        for _attempt in range(1, _v2_result_retries + 1):
            try:
                rows, meta, truncated = await fetch_all_results_v2(
                    client, query_id, max_rows=max_rows
                )
                break
            except TrellixEDRError as exc:
                if exc.status_code == 404 and _attempt < _v2_result_retries:
                    log.warning(
                        "trellix v2 results returned 404 (attempt %d/%d, query_id=%s) "
                        "— retrying in %ds",
                        _attempt,
                        _v2_result_retries,
                        query_id,
                        _v2_result_retry_delay,
                    )
                    await asyncio.sleep(_v2_result_retry_delay)
                else:
                    raise
    else:
        rows, meta, truncated = await fetch_all_results_v1(client, query_id, max_rows=max_rows)

    return query_id, rows, meta, truncated
