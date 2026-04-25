"""gSage AI — ``elk_search`` MCP tool.

Read-only querying of **external** Elasticsearch clusters (typical ELK /
logstash / beats deployments).  Supports three modes:

* ``list_indices`` — enumerate allowed indices on the cluster.
* ``describe_index`` — return the raw mapping for a pattern.
* ``search`` — run a ``_search`` request built from a safe param subset.

Security model
--------------
* **Separate credentials per profile** — gSage's *internal* ES cluster is
  never reused.
* **Hard-coded deny-list** — indices matching internal gSage patterns
  (``gsage_*``, ``.security-*``, ``.kibana*``, etc.) are blocked server-side
  regardless of the admin-provided allow-list.
* **Per-profile allow-list** — empty = deny-all.  Admins must explicitly
  list the index patterns that can be queried.
* **Authentication** — API key only (Kibana → Stack Management).

Result offload
--------------
``search`` results in modes that exceed the inline size budget are offloaded
to MinIO as JSON / CSV / XLSX (default CSV) and returned as a download link.

Permission: ``elk:search``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult, _tool_session_ctx
from src.mcp_server.tools.soc.monitoring.elk_search._cache import (
    build_cache_key,
    get_cache,
)
from src.mcp_server.tools.soc.monitoring.elk_search._client import (
    ElkClient,
    ElkError,
)
from src.mcp_server.tools.soc.monitoring.elk_search._exporters import (
    to_csv,
    to_json,
    to_xlsx,
)
from src.mcp_server.tools.soc.monitoring.elk_search._indices import (
    DENY_PATTERNS,
    collapse_index_patterns,
    filter_indices,
    is_denied,
    match_allowed,
)
from src.mcp_server.tools.soc.monitoring.elk_search._query_builder import (
    QueryBuildError,
    build_query,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ── Size budgets ────────────────────────────────────────────────────────
_MAX_INLINE_HITS = 25          # above this → always offload
_MAX_INLINE_BYTES = 64 * 1024  # inline JSON body hard cap
_DEFAULT_MAX_RESULT_SIZE = 500
_HARD_SIZE_CEILING = 1000      # never allow more than this per call

# ── Config schema ───────────────────────────────────────────────────────
_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["url"],
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "External Elasticsearch cluster URL "
                "(e.g. https://elk.example.com:9200). MUST NOT point to the "
                "gSage internal cluster."
            ),
        },
        "api_key": {
            "type": "string",
            "description": (
                "Base64 API key (id:secret) issued by Kibana → Stack "
                "Management → API keys. Optional: leave empty for "
                "unauthenticated clusters (e.g. lab / internal networks). "
                "Username/password auth is not supported in V1."
            ),
            "sensitive": True,
        },
        "verify_ssl": {
            "type": "boolean",
            "description": "Verify the server TLS certificate (default: true).",
        },
        "ca_cert": {
            "type": "string",
            "description": (
                "Optional path (inside the mcp-server container) to a PEM "
                "CA bundle used to validate the cluster certificate."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "Request timeout in seconds (default: 30).",
        },
        "allowed_index_patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Glob patterns that the LLM is allowed to query "
                "(e.g. ['logstash-*', 'filebeat-*']). An empty list denies "
                "everything. The hard-coded deny-list always applies on top."
            ),
        },
        "max_result_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": _HARD_SIZE_CEILING,
            "description": (
                "Upper bound for 'size' on a single search call "
                f"(default: {_DEFAULT_MAX_RESULT_SIZE}, hard ceiling "
                f"{_HARD_SIZE_CEILING})."
            ),
        },
        "default_time_window_minutes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 43200,
            "description": (
                "Applied to search queries when no time_range is provided "
                "(default: 60 minutes)."
            ),
        },
        "cache_ttl_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3600,
            "description": (
                "TTL for Redis caching of list_indices / describe_index "
                "responses (default: 60; 0 disables caching)."
            ),
        },
        "default_export_format": {
            "type": "string",
            "enum": ["json", "csv", "xlsx"],
            "description": (
                "Format used when offloading large search results to MinIO "
                "(default: csv). Caller can still override per call."
            ),
        },
    },
    "additionalProperties": False,
}

_CONFIG_DEFAULTS: dict = {
    "verify_ssl": True,
    "timeout": 30,
    "max_result_size": _DEFAULT_MAX_RESULT_SIZE,
    "default_time_window_minutes": 60,
    "cache_ttl_seconds": 60,
    "default_export_format": "csv",
    "allowed_index_patterns": [],
}

# ── Params schema ───────────────────────────────────────────────────────
_PARAMS_SCHEMA: dict = {
    "type": "object",
    "required": ["mode"],
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["list_indices", "describe_index", "search"],
            "description": (
                "Operation: 'list_indices' enumerates allowed indices; "
                "'describe_index' returns the mapping of a pattern; "
                "'search' executes a _search request."
            ),
        },
        "pattern": {
            "type": "string",
            "description": (
                "Index or glob pattern (e.g. 'logstash-*'). Required for "
                "'describe_index' and 'search'. Must match the profile's "
                "allow-list and must not match the deny-list."
            ),
        },
        "query_string": {
            "type": "string",
            "description": (
                "Lucene-style query (search mode only). "
                "Example: 'event.action:firewall_block AND source.ip:10.0.0.5'."
            ),
        },
        "filters": {
            "type": "array",
            "description": (
                "Optional AND-filters. Each item: "
                "{field, value} | {field, values:[...]} | {field, exists:true}."
            ),
            "items": {
                "type": "object",
                "required": ["field"],
                "properties": {
                    "field": {"type": "string"},
                    "value": {},
                    "values": {"type": "array"},
                    "exists": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "time_range": {
            "description": (
                "Either a preset string (last_5m, last_15m, last_30m, "
                "last_hour, last_4h, last_12h, last_day, last_week) or an "
                "object {gte: ISO, lte: ISO}. Applied to @timestamp."
            ),
            "oneOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {
                        "gte": {"type": "string"},
                        "lte": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            ],
        },
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Subset of document fields to include in _source. "
                "Recommended to keep responses small."
            ),
        },
        "sort": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "List of 'field:asc' / 'field:desc'. "
                "Default: '@timestamp:desc'."
            ),
        },
        "size": {
            "type": "integer",
            "minimum": 0,
            "maximum": _HARD_SIZE_CEILING,
            "description": (
                "Number of hits to return (search only). Capped by the "
                "profile's max_result_size."
            ),
        },
        "export_format": {
            "type": "string",
            "enum": ["json", "csv", "xlsx"],
            "description": (
                "Format of the offload file (search only). "
                "Defaults to the profile's default_export_format."
            ),
        },
        "inline": {
            "type": "boolean",
            "description": (
                "If true, force an inline response (no offload). "
                "Only honoured when the result fits within the inline size "
                "budget; otherwise it is ignored and the file is offloaded."
            ),
        },
    },
    "additionalProperties": False,
}


class ElkSearchTool(BaseTool):
    """Read-only search on external Elasticsearch clusters.

    Three modes:

    * ``list_indices`` — enumerate indices the profile is allowed to see.
    * ``describe_index`` — fetch the raw mapping for a pattern.
    * ``search`` — run a safe ``_search`` request.

    A hard-coded deny-list protects internal gSage indices (``gsage_*``,
    ``.security-*``, ``.kibana*``, …) even if the admin accidentally grants
    wildcard access.

    Permission: ``elk:search``.
    """

    name: ClassVar[str] = "elk_search"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search and explore external Elasticsearch (ELK) clusters: list "
        "indices, inspect mappings, run filtered queries with CSV/XLSX offload"
    )
    category: ClassVar[str] = "monitoring"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["elk:search"]

    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = _CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = _CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "pattern"}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = _PARAMS_SCHEMA

    # ── Execute ─────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        mode = params.get("mode")

        allow_list: list[str] = list(config.get("allowed_index_patterns") or [])
        cache_ttl = int(config.get("cache_ttl_seconds", 60) or 0)

        client_kwargs = {
            "url": str(config.get("url") or ""),
            "api_key": str(config.get("api_key") or "") or None,
            "verify_ssl": bool(config.get("verify_ssl", True)),
            "ca_cert": config.get("ca_cert") or None,
            "timeout": int(config.get("timeout", 30)),
        }

        # Refuse up-front if profile URL matches the deny-list (rough guard
        # for the internal gSage cluster).  We keep this simple — it only
        # flags obvious mistakes; the real protection is the per-index
        # deny-list enforced below.
        if not client_kwargs["url"]:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_CONFIG",
                "Missing 'url' in the elk_search profile configuration.",
                execution_time_ms=elapsed,
            )

        try:
            if mode == "list_indices":
                data = await self._run_list_indices(
                    agent_context, client_kwargs, allow_list, cache_ttl
                )
            elif mode == "describe_index":
                pattern = self._require_pattern(params)
                self._check_pattern_access(pattern, allow_list)
                data = await self._run_describe_index(
                    agent_context, client_kwargs, pattern, cache_ttl
                )
            elif mode == "search":
                pattern = self._require_pattern(params)
                self._check_pattern_access(pattern, allow_list)
                data = await self._run_search(
                    agent_context, client_kwargs, config, params, pattern
                )
            else:
                elapsed = int((time.monotonic() - t0) * 1000)
                return self._failure(
                    "INVALID_PARAMS",
                    f"Unknown mode: {mode!r}. Expected 'list_indices', "
                    "'describe_index' or 'search'.",
                    execution_time_ms=elapsed,
                )
        except _AccessDenied as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "ACCESS_DENIED", str(exc), execution_time_ms=elapsed
            )
        except QueryBuildError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except ElkError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code, str(exc),
                retryable=exc.retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("elk_search: unexpected error (mode=%s)", mode)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(data, execution_time_ms=elapsed)

    # ── Mode handlers ───────────────────────────────────────────────────

    async def _run_list_indices(
        self,
        agent_context: AgentContext,
        client_kwargs: dict,
        allow_list: list[str],
        cache_ttl: int,
    ) -> dict:
        cache_key = build_cache_key(
            org_id=str(agent_context.org_id),
            profile_id=_profile_key_from_config(client_kwargs),
            mode="list_indices",
            params={"allow_list": sorted(allow_list)},
        )

        cached = await self._cache_get(cache_key, cache_ttl)
        if cached is not None:
            cached["_cached"] = True
            return cached

        async with ElkClient(**client_kwargs) as client:
            raw = await client.list_indices_raw()

        names = [row.get("index") for row in raw if row.get("index")]
        visible = filter_indices(names, allow_list)  # type: ignore[arg-type]
        visible_set = set(visible)
        filtered_rows = [row for row in raw if row.get("index") in visible_set]

        patterns = collapse_index_patterns(visible)

        data = {
            "indices": filtered_rows,
            "patterns": patterns,
            "total": len(filtered_rows),
            "allow_list": list(allow_list),
            "deny_patterns": list(DENY_PATTERNS),
        }
        await self._cache_set(cache_key, data, cache_ttl)
        return data

    async def _run_describe_index(
        self,
        agent_context: AgentContext,
        client_kwargs: dict,
        pattern: str,
        cache_ttl: int,
    ) -> dict:
        cache_key = build_cache_key(
            org_id=str(agent_context.org_id),
            profile_id=_profile_key_from_config(client_kwargs),
            mode="describe_index",
            params={"pattern": pattern},
        )

        cached = await self._cache_get(cache_key, cache_ttl)
        if cached is not None:
            cached["_cached"] = True
            return cached

        async with ElkClient(**client_kwargs) as client:
            mapping = await client.get_mapping(pattern)

        data = {
            "pattern": pattern,
            "mapping": mapping,
            "index_count": len(mapping),
        }
        await self._cache_set(cache_key, data, cache_ttl)
        return data

    async def _run_search(
        self,
        agent_context: AgentContext,
        client_kwargs: dict,
        config: dict,
        params: dict,
        pattern: str,
    ) -> dict:
        max_size = int(config.get("max_result_size", _DEFAULT_MAX_RESULT_SIZE))
        max_size = min(max_size, _HARD_SIZE_CEILING)
        requested_size = int(params.get("size", 50))
        size = min(max(requested_size, 0), max_size)

        # Apply default time window if caller omitted one.
        time_range = params.get("time_range")
        if not time_range:
            default_minutes = int(config.get("default_time_window_minutes", 60))
            time_range = _minutes_to_preset(default_minutes)

        body = build_query(
            query_string=params.get("query_string") or None,
            filters=params.get("filters") or [],
            time_range=time_range,
            fields=params.get("fields") or None,
            sort=params.get("sort") or None,
            size=size,
        )

        async with ElkClient(**client_kwargs) as client:
            resp = await client.search(pattern, body)

        hits_root = resp.get("hits") or {}
        hits = list(hits_root.get("hits") or [])
        total_info = hits_root.get("total") or {}
        total_value = (
            total_info.get("value") if isinstance(total_info, dict) else total_info
        )

        # Choose export format (per-call override, then profile default).
        export_format = str(
            params.get("export_format")
            or config.get("default_export_format")
            or "csv"
        ).lower()
        if export_format not in ("json", "csv", "xlsx"):
            export_format = "csv"

        fields = params.get("fields") or None
        inline_ok = bool(params.get("inline"))

        summary = {
            "pattern": pattern,
            "total_hits": total_value,
            "returned": len(hits),
            "took_ms": resp.get("took"),
            "timed_out": resp.get("timed_out", False),
            "query": body,
        }

        # Always offload when over the inline threshold.
        if len(hits) > _MAX_INLINE_HITS or not inline_ok:
            file_info = await self._offload_hits(
                hits, fields, export_format, pattern, agent_context
            )
            summary["export_format"] = export_format
            summary["file"] = file_info
            summary["hits_preview"] = [
                _hit_preview(h) for h in hits[: min(5, len(hits))]
            ]
            return summary

        # Inline path — ensure JSON body stays within budget.
        inline_hits = [
            {
                "_index": h.get("_index"),
                "_id": h.get("_id"),
                "_score": h.get("_score"),
                "_source": h.get("_source") or {},
            }
            for h in hits
        ]
        import json as _json  # noqa: PLC0415

        try:
            body_bytes = len(_json.dumps(inline_hits, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            body_bytes = _MAX_INLINE_BYTES + 1

        if body_bytes > _MAX_INLINE_BYTES:
            file_info = await self._offload_hits(
                hits, fields, export_format, pattern, agent_context
            )
            summary["export_format"] = export_format
            summary["file"] = file_info
            summary["hits_preview"] = [
                _hit_preview(h) for h in hits[: min(5, len(hits))]
            ]
            summary["note"] = (
                "Response exceeded inline size budget; full results offloaded."
            )
            return summary

        summary["hits"] = inline_hits
        return summary

    # ── Access / pattern helpers ────────────────────────────────────────

    @staticmethod
    def _require_pattern(params: dict) -> str:
        pattern = params.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise QueryBuildError("Parameter 'pattern' is required for this mode.")
        return pattern.strip()

    @staticmethod
    def _check_pattern_access(pattern: str, allow_list: list[str]) -> None:
        if is_denied(pattern):
            raise _AccessDenied(
                f"Pattern '{pattern}' matches the hard-coded deny-list "
                f"and cannot be queried via elk_search."
            )
        if not allow_list:
            raise _AccessDenied(
                "This elk_search profile has no allowed_index_patterns "
                "configured — all queries are denied."
            )
        if not match_allowed(pattern, allow_list):
            raise _AccessDenied(
                f"Pattern '{pattern}' is not in the profile's "
                f"allowed_index_patterns ({allow_list})."
            )

    # ── Offload + cache helpers ─────────────────────────────────────────

    async def _offload_hits(
        self,
        hits: list[dict],
        fields: list[str] | None,
        export_format: str,
        pattern: str,
        agent_context: AgentContext,
    ) -> Optional[dict]:
        if export_format == "json":
            data = to_json(hits)
            content_type = "application/json"
            ext = "json"
        elif export_format == "xlsx":
            data = to_xlsx(hits, fields)
            content_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            ext = "xlsx"
        else:
            data = to_csv(hits, fields)
            content_type = "text/csv"
            ext = "csv"

        safe_pattern = _safe_filename(pattern)
        filename = f"elk_search_{safe_pattern}_{int(time.time())}.{ext}"

        try:
            ctx_session = _tool_session_ctx.get()
            if ctx_session is not None:
                file_info = await self._store_file(
                    data=data,
                    filename=filename,
                    content_type=content_type,
                    agent_context=agent_context,
                    session=ctx_session,
                    description=f"elk_search results for {pattern}",
                )
            else:
                from src.shared.database import _get_session_maker  # noqa: PLC0415

                async with _get_session_maker()() as db_session:
                    file_info = await self._store_file(
                        data=data,
                        filename=filename,
                        content_type=content_type,
                        agent_context=agent_context,
                        session=db_session,
                        description=f"elk_search results for {pattern}",
                    )
            return file_info
        except Exception as exc:
            log.error("elk_search: failed to offload results: %s", exc)
            return None

    @staticmethod
    async def _cache_get(key: str, ttl: int) -> Optional[dict]:
        if ttl <= 0:
            return None
        cache = await get_cache()
        if cache is None:
            return None
        try:
            return await cache.get(key)
        finally:
            await cache.close()

    @staticmethod
    async def _cache_set(key: str, value: dict, ttl: int) -> None:
        if ttl <= 0:
            return
        cache = await get_cache()
        if cache is None:
            return
        try:
            await cache.set(key, value, ttl)
        finally:
            await cache.close()


# ── Module-level helpers ────────────────────────────────────────────────


class _AccessDenied(Exception):
    """Raised when the requested pattern is blocked by deny/allow lists."""


def _profile_key_from_config(client_kwargs: dict) -> str:
    """Stable identifier for the (url, api_key) pair — used in cache keys.

    We cannot use the real profile_id inside :meth:`execute` because the
    framework strips it before calling us.  A short hash of the URL is
    good enough to isolate cache entries between profiles within the same
    org.
    """
    import hashlib as _hashlib  # noqa: PLC0415

    seed = f"{client_kwargs.get('url', '')}|{client_kwargs.get('api_key') or ''}"
    return _hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _minutes_to_preset(minutes: int) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    return {
        "gte": (now - timedelta(minutes=max(1, minutes))).isoformat(),
        "lte": now.isoformat(),
    }


def _safe_filename(value: str) -> str:
    import re as _re  # noqa: PLC0415

    cleaned = _re.sub(r"[^a-zA-Z0-9._-]", "_", value)
    return cleaned[:60] or "elk"


def _hit_preview(hit: dict) -> dict:
    source = hit.get("_source") or {}
    # Keep preview small: first 10 fields.
    trimmed = {k: source[k] for k in list(source)[:10]}
    return {
        "_index": hit.get("_index"),
        "_id": hit.get("_id"),
        "_source": trimmed,
    }
