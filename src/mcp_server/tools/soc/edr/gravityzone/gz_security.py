"""gSage AI — GravityZone Security tool.

Read-only security data from BitDefender GravityZone: blocklist items
and PHASR (Proactive Hardening and Attack Surface Reduction) data.

Supported actions:
    blocklist_items        — List all blocklist entries (hash/path/connection) (API v1.2)
    phasr_recommendations  — Get PHASR recommendations for a company (API v1.0)
    phasr_resources        — List all behavioral profile resources for a company (API v1.0)
    phasr_identities       — List all behavioral profile identities for a company (API v1.0)

Required permission: ``gravityzone:read``
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
    summarize,
)
from src.mcp_server.tools.soc.edr.gravityzone._client import GravityZoneClient, GravityZoneError
from src.mcp_server.tools.soc.edr.gravityzone._export import (
    BLOCKLIST_DEFAULT_GROUP_KEYS,
    PHASR_IDENTITY_DEFAULT_GROUP_KEYS,
    PHASR_RECOMMENDATION_DEFAULT_GROUP_KEYS,
    PHASR_RESOURCE_DEFAULT_GROUP_KEYS,
    enrich_phasr_recommendation,
    normalize_blocklist_item,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ── Shared config schema ──────────────────────────────────────────────────────
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

# ── PHASR mapping dicts (string enum → API int) ───────────────────────────────
_PHASR_CATEGORIES: dict[str, int] = {
    "tampering_tool": 1,
    "hack_tool": 2,
    "remote_tool": 3,
    "miner": 4,
    "lol_bin": 5,
}
_PHASR_ACTION_TAKEN: dict[str, int] = {
    "action_needed": 0,
    "applied": 1,
    "partially_applied": 2,
}
_PHASR_TYPES: dict[str, int] = {
    "allow_access": 0,
    "restrict_access": 1,
    "allow_access_request": 2,
}
_PHASR_SORT_FIELDS: dict[str, str] = {
    "attack_surface_reduction": "attackSurfaceReduction",
    "created_on": "createdOn",
}


class GzSecurityTool(BaseTool):
    """Read security posture data from GravityZone: blocklist and PHASR.

    **Actions:**

    - ``blocklist_items`` — Page through all blocklist entries (hash, path,
      and connection type rules).  Uses API v1.2 which supports all rule types.
    - ``phasr_recommendations`` — Get PHASR behavioral recommendations for a
      company, with optional filters by category, action taken, and type.
    - ``phasr_resources`` — List behavioral profile resources (executables and
      scripts) detected for a company.
    - ``phasr_identities`` — List behavioral profile identities (users and
      service accounts) detected for a company.

    **Examples:**

    - ``"lista todos os itens do blocklist"``
      → action=blocklist_items
    - ``"recomendações PHASR para a empresa abc123"``
      → action=phasr_recommendations, company_id="abc123"
    - ``"recursos PHASR com 'powershell' na empresa abc123"``
      → action=phasr_resources, company_id="abc123", search_string="powershell"

    Permission: ``gravityzone:read``
    """

    name: ClassVar[str] = "gz_security"
    config_namespace: ClassVar[str] = "gravityzone"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Read GravityZone security posture data: endpoint blocklist and PHASR policy recommendations"
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["gravityzone:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

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
                "enum": [
                    "blocklist_items",
                    "phasr_recommendations",
                    "phasr_resources",
                    "phasr_identities",
                ],
                "description": (
                    "Operation to perform:\n"
                    "- blocklist_items: list all blocklist entries (hash/path/connection)\n"
                    "- phasr_recommendations: PHASR recommendations for a company\n"
                    "- phasr_resources: behavioral profile resources for a company\n"
                    "- phasr_identities: behavioral profile identities for a company"
                ),
            },
            "company_id": {
                "type": "string",
                "description": (
                    "Company (organization) ID.  "
                    "Required for all phasr_* actions."
                ),
            },
            "search_string": {
                "type": "string",
                "description": "Search/filter string for phasr_resources and phasr_identities.",
            },
            "categories": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "tampering_tool",
                        "hack_tool",
                        "remote_tool",
                        "miner",
                        "lol_bin",
                    ],
                },
                "description": (
                    "Filter PHASR recommendations by threat category "
                    "(action=phasr_recommendations only).  "
                    "Values: tampering_tool, hack_tool, remote_tool, miner, lol_bin."
                ),
            },
            "action_taken": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["action_needed", "applied", "partially_applied"],
                },
                "description": (
                    "Filter PHASR recommendations by applied status "
                    "(action=phasr_recommendations only).  "
                    "Values: action_needed, applied, partially_applied."
                ),
            },
            "recommendation_type": {
                "type": "string",
                "enum": ["allow_access", "restrict_access", "allow_access_request"],
                "description": (
                    "Filter PHASR recommendations by type "
                    "(action=phasr_recommendations only).  "
                    "Values: allow_access, restrict_access, allow_access_request."
                ),
            },
            "sort": {
                "type": "string",
                "enum": ["attack_surface_reduction", "created_on"],
                "description": "Sort PHASR recommendations by field (default: created_on).",
            },
            "dir": {
                "type": "string",
                "enum": ["ASC", "DESC"],
                "description": "Sort direction for PHASR recommendations (default: DESC).",
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": (
                    "First page to fetch (default: 1). With max_pages > 1, "
                    "successive pages are walked starting from this offset."
                ),
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 30,
                "description": "Items per page (max 100, default: 30).",
            },
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 1,
                "description": (
                    "Maximum pages to fetch (default: 1 \u2014 single page). Increase "
                    "to auto-paginate up to per_page \u00d7 max_pages records. The "
                    "shared overflow logic still caps the inline preview at "
                    f"{AGENT_PREVIEW_ROWS} rows."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all fetched rows as a CSV artifact. PREFER CSV when the "
                    "user asks to 'save', 'export' or 'download' the results \u2014 "
                    "it is the natural format for tabular data. CSV is also "
                    f"generated automatically when more than {AGENT_PREVIEW_ROWS} "
                    "rows are returned."
                ),
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all fetched rows as a JSON artifact. Use only when "
                    "the user explicitly asks for JSON \u2014 otherwise prefer "
                    "'export_csv'."
                ),
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of column names for top-N analytics. "
                    "When omitted, an action-specific default set is used."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Top-N size for each grouped column (default: 10).",
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
                if action == "blocklist_items":
                    result = await self._blocklist_items(client, params, agent_context)
                elif action == "phasr_recommendations":
                    result = await self._phasr_recommendations(client, params, agent_context)
                elif action == "phasr_resources":
                    result = await self._phasr_resources(client, params, agent_context)
                elif action == "phasr_identities":
                    result = await self._phasr_identities(client, params, agent_context)
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
            log.exception("gz_security: unexpected error (action=%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(result, execution_time_ms=elapsed)

    # ── Action handlers ────────────────────────────────────────────────────

    async def _paginate(
        self,
        client: GravityZoneClient,
        service: str,
        method: str,
        rpc_params: dict,
        *,
        per_page: int,
        max_pages: int,
        start_page: int = 1,
        api_version: str = "v1.0",
    ) -> tuple[list[Any], dict]:
        """Fetch up to ``per_page * max_pages`` items, returning (items, meta).

        ``meta`` carries the upstream paging hints from the *first* page
        (``total``, ``pagesCount``) plus a synthesised ``truncated`` flag set
        when ``hasMoreRecords`` was still true at the cap.
        """
        all_items: list[Any] = []
        meta: dict[str, Any] = {}
        truncated = False
        for offset in range(max_pages):
            page_num = start_page + offset
            rpc_params["page"] = page_num
            rpc_params["perPage"] = per_page
            result = await client.call(
                service, method, rpc_params, api_version=api_version
            )
            if not isinstance(result, dict):
                break
            if offset == 0:
                meta = {
                    "total": result.get("total", 0),
                    "pages_count": result.get("pagesCount", 1),
                    "per_page": result.get("perPage", per_page),
                }
            items = result.get("items", [])
            if isinstance(items, list):
                all_items.extend(items)
            if not result.get("hasMoreRecords", False):
                break
            if offset == max_pages - 1:
                truncated = True
        meta["truncated"] = truncated
        return all_items, meta

    async def _blocklist_items(
        self,
        client: GravityZoneClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        per_page = min(int(params.get("per_page", 30)), 100)
        max_pages = min(int(params.get("max_pages", 1)), 50)

        rpc_params: dict = {}
        # v1.2: supports hash, path, and connection type blocklist entries
        items, meta = await self._paginate(
            client,
            "incidents",
            "getBlocklistItems",
            rpc_params,
            per_page=per_page,
            max_pages=max_pages,
            start_page=int(params.get("page", 1)),
            api_version="v1.2",
        )

        rows = [normalize_blocklist_item(it) for it in items if isinstance(it, dict)]
        return await self._build_list_payload(
            action="blocklist_items",
            rows=rows,
            params=params,
            agent_context=agent_context,
            meta=meta,
            default_keys=BLOCKLIST_DEFAULT_GROUP_KEYS,
            filename_prefix="gz_blocklist_items",
        )

    async def _phasr_recommendations(
        self,
        client: GravityZoneClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        company_id = params.get("company_id", "").strip()
        if not company_id:
            raise GravityZoneError(
                "company_id is required for action=phasr_recommendations.", code=-32602
            )

        rpc_params: dict = {"companyId": company_id}

        # Map string enum arrays to int arrays
        if params.get("categories"):
            rpc_params["category"] = [
                _PHASR_CATEGORIES[c] for c in params["categories"]
                if c in _PHASR_CATEGORIES
            ]
        if params.get("action_taken"):
            rpc_params["actionTaken"] = [
                _PHASR_ACTION_TAKEN[a] for a in params["action_taken"]
                if a in _PHASR_ACTION_TAKEN
            ]
        if params.get("recommendation_type"):
            rt = params["recommendation_type"]
            if rt in _PHASR_TYPES:
                rpc_params["type"] = _PHASR_TYPES[rt]

        if params.get("sort"):
            sort_key = params["sort"]
            rpc_params["sort"] = _PHASR_SORT_FIELDS.get(sort_key, "createdOn")
        if params.get("dir"):
            rpc_params["dir"] = params["dir"]

        per_page = min(int(params.get("per_page", 30)), 100)
        max_pages = min(int(params.get("max_pages", 1)), 50)

        items, meta = await self._paginate(
            client,
            "phasr",
            "getPhasrRecommendations",
            rpc_params,
            per_page=per_page,
            max_pages=max_pages,
            start_page=int(params.get("page", 1)),
        )
        rows = [enrich_phasr_recommendation(it) for it in items if isinstance(it, dict)]
        return await self._build_list_payload(
            action="phasr_recommendations",
            rows=rows,
            params=params,
            agent_context=agent_context,
            meta=meta,
            default_keys=PHASR_RECOMMENDATION_DEFAULT_GROUP_KEYS,
            filename_prefix=f"gz_phasr_recommendations_{company_id}",
        )

    async def _phasr_resources(
        self,
        client: GravityZoneClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        company_id = params.get("company_id", "").strip()
        if not company_id:
            raise GravityZoneError(
                "company_id is required for action=phasr_resources.", code=-32602
            )

        rpc_params: dict = {"companyId": company_id}
        if params.get("search_string"):
            rpc_params["searchString"] = params["search_string"]

        per_page = min(int(params.get("per_page", 30)), 100)
        max_pages = min(int(params.get("max_pages", 1)), 50)

        items, meta = await self._paginate(
            client,
            "phasr",
            "getAllCompanyResources",
            rpc_params,
            per_page=per_page,
            max_pages=max_pages,
            start_page=int(params.get("page", 1)),
        )
        rows = [it for it in items if isinstance(it, dict)]
        return await self._build_list_payload(
            action="phasr_resources",
            rows=rows,
            params=params,
            agent_context=agent_context,
            meta=meta,
            default_keys=PHASR_RESOURCE_DEFAULT_GROUP_KEYS,
            filename_prefix=f"gz_phasr_resources_{company_id}",
        )

    async def _phasr_identities(
        self,
        client: GravityZoneClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        company_id = params.get("company_id", "").strip()
        if not company_id:
            raise GravityZoneError(
                "company_id is required for action=phasr_identities.", code=-32602
            )

        rpc_params: dict = {"companyId": company_id}
        if params.get("search_string"):
            rpc_params["searchString"] = params["search_string"]

        per_page = min(int(params.get("per_page", 30)), 100)
        max_pages = min(int(params.get("max_pages", 1)), 50)

        items, meta = await self._paginate(
            client,
            "phasr",
            "getAllCompanyIdentities",
            rpc_params,
            per_page=per_page,
            max_pages=max_pages,
            start_page=int(params.get("page", 1)),
        )
        rows = [it for it in items if isinstance(it, dict)]
        return await self._build_list_payload(
            action="phasr_identities",
            rows=rows,
            params=params,
            agent_context=agent_context,
            meta=meta,
            default_keys=PHASR_IDENTITY_DEFAULT_GROUP_KEYS,
            filename_prefix=f"gz_phasr_identities_{company_id}",
        )

    async def _build_list_payload(
        self,
        *,
        action: str,
        rows: list[dict],
        params: dict,
        agent_context: AgentContext,
        meta: dict,
        default_keys: tuple[str, ...],
        filename_prefix: str,
    ) -> dict:
        """Common assembly for paginated list-style actions.

        Runs the top-N summariser, calls the shared
        :func:`build_agent_payload` (which caps the inline preview and
        force-generates a CSV on overflow) and returns the merged dict.
        """
        summary = summarize(
            rows,
            group_by=params.get("group_by") or None,
            top_n=int(params.get("top_n", 10) or 10),
            default_keys=default_keys,
        )
        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=filename_prefix,
            agent_context=agent_context,
        )
        out: dict = {
            "action": action,
            "total": meta.get("total", len(rows)),
            "pages_count": meta.get("pages_count", 1),
            "per_page": meta.get("per_page"),
            "fetched": len(rows),
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "rows_preview_limit": AGENT_PREVIEW_ROWS,
            "agent_hint": agent_payload["agent_hint"],
            "artifacts": agent_payload["artifacts"],
            "summary": summary,
            "items": agent_payload["rows_preview"],
        }
        if meta.get("truncated"):
            out["coverage_warning"] = (
                "max_pages cap reached before all records were fetched. "
                "Increase max_pages or narrow the query for full coverage."
            )
        return out
