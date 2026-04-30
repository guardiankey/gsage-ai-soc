"""gSage AI — GravityZone Endpoints tool.

Lists and retrieves endpoint information from the BitDefender GravityZone
network inventory via the JSON-RPC API.

Supported actions:
    list    — List managed/unmanaged endpoints with optional filters (API v1.1)
    details — Retrieve full details for a specific endpoint by ID (API v1.0)

Required permission: ``gravityzone:read``
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.gravityzone._client import GravityZoneClient, GravityZoneError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ── Shared config schema (identical for all gz_* tools) ──────────────────────
_GZ_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "api_key": {
            "type": "string",
            "description": "GravityZone API key (from Control Center → My Account → API keys).",
            "sensitive": True,
        },
        "base_url": {
            "type": "string",
            "description": (
                "GravityZone API base URL.  "
                "Default: https://cloud.gravityzone.bitdefender.com/api.  "
                "Override for on-premise deployments."
            ),
        },
    },
    "additionalProperties": False,
}
_GZ_CONFIG_DEFAULTS: dict = {
    "base_url": "https://cloud.gravityzone.bitdefender.com/api",
}

# Machine type codes
_MACHINE_TYPES = {0: "other", 1: "computer", 2: "virtual_machine", 3: "ec2_instance"}


class GzEndpointsTool(BaseTool):
    """Query the GravityZone network inventory for endpoints.

    **Actions:**

    - ``list`` — Page through all endpoints under a company or group,
      optionally filtered by name, IP, MAC, or managed status.
      Uses API v1.1 (supports up to 1000 items/page).
    - ``details`` — Full endpoint record for a specific ``endpoint_id``
      (OS, IPs, MACs, FQDN, policy, protection modules, risk score, etc.).

    **Examples:**

    - ``"lista todos os endpoints gerenciados"``
      → action=list, is_managed=true
    - ``"busca endpoints com IP 192.168.1.10"``
      → action=list, filter_ip="192.168.1.10"
    - ``"endpoints na rede 10.0.1.0/24"``
      → action=list, filter_cidr="10.0.1.0/24"
    - ``"detalhes do endpoint abc123"``
      → action=details, endpoint_id="abc123"

    Permission: ``gravityzone:read``
    """

    name: ClassVar[str] = "gz_endpoints"
    config_namespace: ClassVar[str] = "gravityzone"
    version: ClassVar[str] = "2.0.0"
    summary: ClassVar[str] = "Query GravityZone network inventory for endpoint details, managed devices, and installation info"
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["gravityzone:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {"target_entities": "endpoint_id"}
    audit_output: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = _GZ_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = _GZ_CONFIG_DEFAULTS
    requires_config: ClassVar[bool] = False

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "details"],
                "description": (
                    "Operation to perform:\n"
                    "- list: paginate endpoints (supports filters, up to 1000/page via API v1.1)\n"
                    "- details: full record for one endpoint_id"
                ),
            },
            "endpoint_id": {
                "type": "string",
                "description": "Endpoint object ID.  Required for action=details.",
            },
            "parent_id": {
                "type": "string",
                "description": (
                    "Company or group ID to scope the query.  "
                    "Defaults to the company linked to the API key."
                ),
            },
            "is_managed": {
                "type": "boolean",
                "description": (
                    "Filter by managed status (action=list only).  "
                    "true=only managed, false=only unmanaged, omit=all."
                ),
            },
            "filter_name": {
                "type": "string",
                "minLength": 3,
                "description": (
                    "Filter endpoints by name (partial match, min 3 chars).  "
                    "Prefix with * for suffix search (e.g. '*-server')."
                ),
            },
            "filter_ip": {
                "type": "string",
                "description": "Filter by exact endpoint IP address (applied client-side).",
            },
            "filter_cidr": {
                "type": "string",
                "description": (
                    "Filter endpoints whose primary IP falls within a CIDR range "
                    "(e.g. '192.168.1.0/24', '10.0.0.0/8').  Applied client-side after "
                    "fetching all pages.  When set, per_page is forced to 1000 to minimise "
                    "round-trips."
                ),
            },
            "filter_mac": {
                "type": "string",
                "description": "Filter by MAC address (e.g. 'AA:BB:CC:DD:EE:FF').",
            },
            "include_scan_logs": {
                "type": "boolean",
                "description": "Include last successful scan info in the response.",
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "First page to retrieve (action=list, default: 1).",
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "default": 100,
                "description": "Items per page (max 1000 via API v1.1, default: 100).",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
                "description": "Maximum pages to fetch for action=list (default: 5).",
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = params["action"]

        try:
            async with GravityZoneClient(
                api_key=config.get("api_key") or None,
                base_url=config.get("base_url") or None,
            ) as client:
                if action == "list":
                    result = await self._list_endpoints(client, params)
                elif action == "details":
                    result = await self._endpoint_details(client, params)
                else:
                    return self._failure("INVALID_ACTION", f"Unknown action: {action}")
        except GravityZoneError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(
                f"GZ_ERROR_{exc.code}" if exc.code else "GZ_ERROR",
                str(exc),
                retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("gz_endpoints: unexpected error (action=%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(result, execution_time_ms=elapsed)

    # ── Action handlers ────────────────────────────────────────────────────

    async def _list_endpoints(self, client: GravityZoneClient, params: dict) -> dict:
        rpc_params: dict = {}
        if "parent_id" in params:
            rpc_params["parentId"] = params["parent_id"]
        if "is_managed" in params:
            rpc_params["isManaged"] = params["is_managed"]

        # Build filters sub-object
        filters: dict = {}
        details_filter: dict = {}
        if "filter_name" in params:
            details_filter["name"] = params["filter_name"]
        if "filter_mac" in params:
            details_filter["macs"] = [params["filter_mac"]]
        if details_filter:
            filters["details"] = details_filter
        if filters:
            rpc_params["filters"] = filters

        # Options
        if params.get("include_scan_logs"):
            rpc_params["options"] = {"includeScanLogs": True}

        # When CIDR filtering is requested, maximise page size to reduce round-trips
        cidr_filter = params.get("filter_cidr")
        default_per_page = 1000 if cidr_filter else 100
        per_page = min(int(params.get("per_page", default_per_page)), 1000)
        max_pages = min(int(params.get("max_pages", 5)), 20)
        start_page = int(params.get("page", 1))

        all_items: list = []
        total = 0
        pages_count = 0
        truncated = False
        for page_num in range(start_page, start_page + max_pages):
            rpc_params["page"] = page_num
            rpc_params["perPage"] = per_page
            # v1.1: supports up to 1000 per page and returns hasMoreRecords
            result = await client.call(
                "network", "getEndpointsList", rpc_params, api_version="v1.1"
            )
            if not isinstance(result, dict):
                break
            if page_num == start_page:
                total = result.get("total", 0)
                pages_count = result.get("pagesCount", 1)
            all_items.extend(result.get("items", []))
            if not result.get("hasMoreRecords", False):
                break
            if page_num == start_page + max_pages - 1:
                # Reached max_pages cap but there are more records
                truncated = result.get("hasMoreRecords", False)

        # Post-filter by exact IP (GravityZone does not support IP filter natively)
        if "filter_ip" in params:
            ip_filter = params["filter_ip"]
            all_items = [e for e in all_items if e.get("ip") == ip_filter]

        # Post-filter by CIDR range
        if cidr_filter:
            all_items = _filter_by_cidr(all_items, cidr_filter)

        out: dict = {
            "action": "list",
            "total": total,
            "pages_count": pages_count,
            "fetched": len(all_items),
            "endpoints": [_normalize_endpoint(e) for e in all_items],
        }
        if truncated:
            out["coverage_warning"] = (
                f"max_pages ({max_pages}) reached before all {total} endpoints were fetched. "
                "Results may be incomplete — increase max_pages or narrow the query."
            )
        return out

    async def _endpoint_details(self, client: GravityZoneClient, params: dict) -> dict:
        endpoint_id = params.get("endpoint_id", "").strip()
        if not endpoint_id:
            raise GravityZoneError(
                "endpoint_id is required for action=details.", code=-32602
            )
        rpc_params: dict = {"endpointId": endpoint_id}
        if params.get("include_scan_logs"):
            rpc_params["options"] = {"includeScanLogs": True}
        result = await client.call("network", "getManagedEndpointDetails", rpc_params)
        if not isinstance(result, dict):
            return {"action": "details", "endpoint": None}
        return {"action": "details", "endpoint": result}


# ── Helpers ────────────────────────────────────────────────────────────────

def _filter_by_cidr(items: list[dict], cidr: str) -> list[dict]:
    """Return only items whose primary IP falls within *cidr*.

    Invalid or missing IPs are silently excluded.
    Accepts both host addresses (192.168.1.5) and network notation
    (192.168.1.0/24).  ``strict=False`` allows host bits to be set.
    """
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        log.warning("gz_endpoints: invalid filter_cidr %r — ignoring", cidr)
        return items

    result = []
    for item in items:
        ip_str = item.get("ip")
        if not ip_str:
            continue
        try:
            if ipaddress.ip_address(ip_str) in network:
                result.append(item)
        except ValueError:
            continue
    return result


# ── Normalizer ─────────────────────────────────────────────────────────────

def _normalize_endpoint(raw: dict) -> dict:
    machine_type_int = raw.get("machineType", 0)
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "label": raw.get("label"),
        "fqdn": raw.get("fqdn"),
        "ip": raw.get("ip"),
        "macs": raw.get("macs", []),
        "group_id": raw.get("groupId"),
        "is_managed": raw.get("isManaged"),
        "machine_type": _MACHINE_TYPES.get(machine_type_int, "other"),
        "os_version": raw.get("operatingSystemVersion"),
        "managed_with_best": raw.get("managedWithBest"),
        "is_container_host": raw.get("isContainerHost"),
        "managed_relay": raw.get("managedRelay"),
        "security_server": raw.get("securityServer"),
        "product_outdated": raw.get("productOutdated"),
        "policy": raw.get("policy"),
        "last_successful_scan": raw.get("lastSuccessfulScan"),
        "ssid": raw.get("ssid"),
    }
