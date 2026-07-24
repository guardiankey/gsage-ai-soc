"""gSage AI — MISP Search tool.

Hybrid unified search over the MISP threat intelligence platform.
The primary parameter is ``query`` — the tool decides automatically where
to search (events, attributes, tags, galaxies, IOCs) and returns consolidated,
ranked results. Use the optional ``scope`` parameter for domain-specific searches.

Required permission: ``misp:read``
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult, _tool_session_ctx
from src.mcp_server.tools.result_export import AGENT_PREVIEW_ROWS, build_agent_payload
from src.mcp_server.tools.soc.threat_intel.misp import _query as Q
from src.mcp_server.tools.soc.threat_intel.misp._client import MISPClient, MISPError
from src.shared.cache.decorator import cached
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_TOOL_NAME = "misp_search"
_CACHE_TTL = Q.CACHE_TTL_SEARCH

# Scope enum values
_SCOPES = [
    "ioc", "event", "attribute", "object",
    "tag", "galaxy", "galaxy_cluster",
    "feed", "taxonomy", "warninglist",
    "sighting", "event_report", "organisation",
]


class MispSearchTool(BaseTool):
    """Hybrid unified search over MISP.

    Uses ``query`` as the primary search parameter. When ``scope`` is
    omitted, performs hybrid parallel search across events, attributes,
    tags, galaxies and reports, consolidating and ranking results.

    Permission: ``misp:read``
    """

    name: ClassVar[str] = "misp_search"
    config_namespace: ClassVar[str] = "misp"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Hybrid unified MISP search: events, attributes, IOCs, tags, "
        "galaxies. Use 'query' for automatic hybrid search; 'scope' for "
        "domain-specific queries."
    )
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["misp:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.MISP_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.MISP_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Main search term. Can be an IOC (IP, domain, hash), "
                    "threat actor name ('APT29'), malware ('Cobalt Strike'), "
                    "CVE, tag, or free text. Hybrid search is used when "
                    "'scope' is omitted."
                ),
            },
            "scope": {
                "type": "string",
                "enum": _SCOPES,
                "description": (
                    "Restrict search to a specific domain (optional). "
                    "If omitted, automatic hybrid search is performed."
                ),
            },
            "identifier": {
                "type": "string",
                "description": (
                    "Numeric ID or UUID of an event for scope='event'. "
                    "The tool auto-detects the format."
                ),
            },
            "tag": {
                "type": "string",
                "description": "Filter by exact tag. E.g. 'tlp:amber', 'apt29', 'ransomware'.",
            },
            "galaxy_id": {
                "type": "integer",
                "description": "Galaxy ID.",
            },
            "cluster_id": {
                "type": "integer",
                "description": "Galaxy cluster ID.",
            },
            "org": {
                "type": "string",
                "description": "Filter by organisation (name or ID).",
            },
            "date_from": {
                "type": "string",
                "description": "Start date (YYYY-MM-DD).",
            },
            "date_to": {
                "type": "string",
                "description": "End date (YYYY-MM-DD).",
            },
            "threat_level": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 4},
                "description": "Filter by threat level(s): 1=High, 2=Medium, 3=Low, 4=Undefined.",
            },
            "published": {
                "type": "boolean",
                "description": "Filter published events only (default: false = all).",
            },
            "enrich": {
                "type": "boolean",
                "default": True,
                "description": "Enrich results with tags, galaxies, ATT&CK and related organisations.",
            },
            "correlate": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Execute textual correlation summary (scope='ioc' only): "
                    "returns related events and sibling IOCs as enriched text. "
                    "Limited to 2 levels. For explicit graph (nodes/edges), "
                    "use misp_analyze(action='correlation_graph')."
                ),
            },
            "max_events": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_EVENTS,
                "default": Q.DEFAULT_MAX_EVENTS,
                "description": "Hard limit of events returned.",
            },
            "max_attributes_per_event": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ATTRIBUTES_PER_EVENT,
                "default": Q.DEFAULT_MAX_ATTRIBUTES_PER_EVENT,
                "description": "Limit of attributes included per event during enrichment.",
            },
            "max_related_iocs": {
                "type": "integer",
                "minimum": 0,
                "maximum": Q.HARD_MAX_RELATED_IOCS,
                "default": Q.DEFAULT_MAX_RELATED_IOCS,
                "description": "Limit of related IOCs returned per event.",
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "Results page.",
            },
            "export_csv": {"type": "boolean", "default": False},
            "export_json": {"type": "boolean", "default": False},
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
        query = str(params.get("query", "")).strip()
        scope = params.get("scope")

        if not query and scope not in ("feed", "taxonomy", "warninglist"):
            return self._failure(
                "VALIDATION_ERROR",
                "Parameter 'query' is required unless listing feeds, taxonomies, or warninglists.",
                retryable=False,
            )

        try:
            client = MISPClient(
                url=config["url"],
                api_key=config["api_key"],
                verify_cert=config.get("verify_cert", True),
            )

            if scope is None:
                result_data = await self._hybrid_search(
                    client, query, params, agent_context
                )
            elif scope == "ioc":
                result_data = await self._search_ioc(
                    client, query, params, agent_context
                )
            elif scope == "event":
                result_data = await self._search_event(
                    client, query, params, agent_context
                )
            elif scope == "attribute":
                result_data = await self._search_attribute(
                    client, query, params
                )
            elif scope == "object":
                result_data = await self._search_object(
                    client, query, params
                )
            elif scope == "tag":
                result_data = await self._search_tag(client, query, params)
            elif scope in ("galaxy", "galaxy_cluster"):
                result_data = await self._search_galaxy(
                    client, query, params, scope
                )
            elif scope == "feed":
                result_data = await self._search_feed(client, params)
            elif scope == "taxonomy":
                result_data = await self._search_taxonomy(client, query)
            elif scope == "warninglist":
                result_data = await self._search_warninglist(
                    client, query, params
                )
            elif scope == "sighting":
                result_data = await self._search_sighting(
                    client, query, params
                )
            elif scope == "event_report":
                result_data = await self._search_event_report(
                    client, query, params
                )
            elif scope == "organisation":
                result_data = await self._search_organisation(
                    client, query
                )
            else:
                return self._failure(
                    "VALIDATION_ERROR",
                    f"Unknown scope: {scope}",
                    retryable=False,
                )

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._success(result_data, execution_time_ms=elapsed_ms)

        except MISPError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                exc.message,
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.exception("Unexpected error in misp_search")
            return self._failure(
                "INTERNAL_ERROR",
                str(exc),
                retryable=False,
                execution_time_ms=elapsed_ms,
            )

    # ── Hybrid search ──────────────────────────────────────────────────

    async def _hybrid_search(
        self,
        client: MISPClient,
        query: str,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        """Parallel hybrid search across all MISP domains."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        max_attrs = int(params.get("max_attributes_per_event", Q.DEFAULT_MAX_ATTRIBUTES_PER_EVENT))
        max_related = int(params.get("max_related_iocs", Q.DEFAULT_MAX_RELATED_IOCS))

        # Run searches in parallel
        event_search = asyncio.create_task(
            client.search("events", eventinfo=query, limit=max_events, metadata=True)
        )
        attr_search = asyncio.create_task(
            client.search("attributes", value=query, limit=max_events)
        )
        # Also try as tag
        tag_search = asyncio.create_task(
            client.search("tags", searchFor=query, limit=20)
        )
        # Try as galaxy cluster name
        galaxy_search = asyncio.create_task(
            client.search("galaxy_clusters", searchFor=query, limit=10)
        )

        results = await asyncio.gather(
            event_search, attr_search, tag_search, galaxy_search,
            return_exceptions=True,
        )

        events_raw = results[0] if not isinstance(results[0], BaseException) else []
        attributes_raw = results[1] if not isinstance(results[1], BaseException) else []
        tags_raw = results[2] if not isinstance(results[2], BaseException) else []
        galaxies_raw = results[3] if not isinstance(results[3], BaseException) else []

        # Normalize
        events_list = events_raw if isinstance(events_raw, list) else events_raw.get("response", events_raw.get("Event", []))

        normalized_events: list[dict] = []
        seen_ids: set[int] = set()

        for event in (events_list if isinstance(events_list, list) else []):
            if not isinstance(event, dict):
                continue
            eid = event.get("id") or event.get("Event", {}).get("id")
            if not eid or eid in seen_ids:
                continue
            seen_ids.add(eid)
            normalized = Q.normalize_event(event, max_attributes=max_attrs)
            normalized_events.append(normalized)
            if len(normalized_events) >= max_events:
                break

        # Build hybrid summary
        enriched = params.get("enrich", True)
        summary = Q.build_ioc_summary(normalized_events, []) if enriched else {}

        total_results = len(normalized_events)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": "hybrid",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "result_breakdown": {
                "events_found": len(normalized_events),
                "attributes_found": len(attributes_raw) if isinstance(attributes_raw, list) else 0,
                "tags_found": len(tags_raw) if isinstance(tags_raw, list) else 0,
                "galaxies_found": len(galaxies_raw) if isinstance(galaxies_raw, list) else 0,
            },
            "hybrid_summary": summary,
            "events": normalized_events[:max_events],
        }

    # ── IOC search ─────────────────────────────────────────────────────

    async def _search_ioc(
        self,
        client: MISPClient,
        query: str,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        """Search for an IOC and return enriched context."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        max_attrs = int(params.get("max_attributes_per_event", Q.DEFAULT_MAX_ATTRIBUTES_PER_EVENT))
        max_related = int(params.get("max_related_iocs", Q.DEFAULT_MAX_RELATED_IOCS))
        enrich = params.get("enrich", True)
        correlate = params.get("correlate", False)

        ioc_type = Q.detect_ioc_type(query)

        # Search attributes
        attr_result = await client.search(
            "attributes", value=query, limit=max_events, includeEventTags=True,
        )

        # Normalize
        raw_attrs = attr_result if isinstance(attr_result, list) else (
            attr_result.get("response", {}).get("Attribute", [])
            if isinstance(attr_result, dict) else []
        )
        attrs: list[dict] = [Q.normalize_attribute(a) for a in raw_attrs if isinstance(a, dict)]

        event_ids: set[int] = set()
        for attr in attrs:
            eid = attr.get("event_id")
            if eid:
                event_ids.add(int(eid))

        # Fetch full events
        events: list[dict] = []
        related_iocs_by_event: dict[int, list[dict]] = {}
        for eid in sorted(event_ids)[:max_events]:
            try:
                full_event = await client.get_event(eid)
                normalized = Q.normalize_event(
                    full_event, max_attributes=max_attrs
                )

                # Collect related IOCs from the same event
                if correlate or enrich:
                    related = []
                    raw_event_attrs = full_event.get("Event", full_event).get("Attribute", [])
                    for ra in raw_event_attrs:
                        if not isinstance(ra, dict):
                            continue
                        if ra.get("value") != query:
                            related.append({
                                "value": ra.get("value", ""),
                                "type": ra.get("type", ""),
                            })
                    related_iocs_by_event[eid] = related[:max_related]

                if related_iocs_by_event.get(eid):
                    normalized["related_iocs"] = related_iocs_by_event[eid]

                events.append(normalized)
            except MISPError:
                continue

        # Build summary
        summary = Q.build_ioc_summary(events, attrs) if enrich else {}

        total_results = len(events)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "ioc_type": ioc_type,
            "found": len(events) > 0,
            "occurrences": len(events),
            "search_type": "ioc",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "events": events[:max_events],
            "summary": summary,
        }

    # ── Event search ───────────────────────────────────────────────────

    async def _search_event(
        self,
        client: MISPClient,
        query: str,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        """Search events by keyword, tag, org, dates, or identifier."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        max_attrs = int(params.get("max_attributes_per_event", Q.DEFAULT_MAX_ATTRIBUTES_PER_EVENT))
        enrich = params.get("enrich", True)
        identifier = params.get("identifier")

        # If identifier is provided, try direct fetch
        if identifier:
            try:
                event = await client.get_event(identifier)
                normalized = Q.normalize_event(event, max_attributes=max_attrs)  # type: ignore[assignment]
                return {
                    "query": query or str(identifier),
                    "identifier": identifier,
                    "search_type": "event",
                    "total_results": 1,
                    "total_pages": 1,
                    "current_page": 1,
                    "has_next": False,
                    "truncated": False,
                    "events": [normalized],
                }
            except MISPError as exc:
                if exc.code == "NOT_FOUND":
                    return {
                        "query": query or str(identifier),
                        "identifier": identifier,
                        "search_type": "event",
                        "total_results": 0,
                        "total_pages": 0,
                        "current_page": 1,
                        "has_next": False,
                        "truncated": False,
                        "events": [],
                    }
                raise

        # Build search kwargs
        search_kwargs: dict[str, Any] = {"limit": max_events, "metadata": True}
        if query:
            search_kwargs["eventinfo"] = query
        if params.get("tag"):
            search_kwargs["tags"] = [params["tag"]]
        if params.get("org"):
            search_kwargs["org"] = params["org"]
        if params.get("date_from"):
            search_kwargs["date_from"] = params["date_from"]
        if params.get("date_to"):
            search_kwargs["date_to"] = params["date_to"]
        if params.get("published") is not None:
            search_kwargs["published"] = params["published"]

        result = await client.search("events", **search_kwargs)
        raw_events = result if isinstance(result, list) else (
            result.get("response", result.get("Event", []))
            if isinstance(result, dict) else []
        )

        normalized: list[dict] = []
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            normalized.append(Q.normalize_event(event, max_attributes=max_attrs))

        total_results = len(normalized)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": "event",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "events": normalized[:max_events],
        }

    # ── Attribute search ───────────────────────────────────────────────

    async def _search_attribute(
        self,
        client: MISPClient,
        query: str,
        params: dict,
    ) -> dict:
        """Search attributes by value, type, tag, or event."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        search_kwargs: dict[str, Any] = {"limit": max_events}
        if query:
            search_kwargs["value"] = query
        if params.get("tag"):
            search_kwargs["tags"] = [params["tag"]]

        result = await client.search("attributes", **search_kwargs)
        raw_attrs = result if isinstance(result, list) else (
            result.get("response", {}).get("Attribute", [])
            if isinstance(result, dict) else []
        )
        attrs = [Q.normalize_attribute(a) for a in raw_attrs if isinstance(a, dict)]

        total_results = len(attrs)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": "attribute",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "attributes": attrs[:max_events],
        }

    # ── Object search ──────────────────────────────────────────────────

    async def _search_object(
        self,
        client: MISPClient,
        query: str,
        params: dict,
    ) -> dict:
        """Search MISP objects by name, template, event, or UUID."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        result = await client.search("objects", object_name=query, limit=max_events)
        raw_objects = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        objects_list: list[dict] = []
        for obj in raw_objects:
            if isinstance(obj, dict):
                objects_list.append({
                    "id": obj.get("id"),
                    "uuid": obj.get("uuid"),
                    "name": obj.get("name"),
                    "meta-category": obj.get("meta-category"),
                    "description": obj.get("description", ""),
                    "template_uuid": obj.get("template_uuid"),
                    "template_version": obj.get("template_version"),
                })

        total_results = len(objects_list)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": "object",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "objects": objects_list[:max_events],
        }

    # ── Tag search ─────────────────────────────────────────────────────

    async def _search_tag(
        self,
        client: MISPClient,
        query: str,
        params: dict,
    ) -> dict:
        """Search tags by name (substring match)."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        result = await client.search("tags", searchFor=query, limit=max_events)
        raw_tags = result if isinstance(result, list) else (
            result.get("response", result.get("Tag", []))
            if isinstance(result, dict) else []
        )

        tags_list: list[dict] = []
        for t in raw_tags:
            if isinstance(t, dict):
                tags_list.append({
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "colour": t.get("colour"),
                    "exportable": t.get("exportable"),
                    "count": t.get("count", 0),
                })

        total_results = len(tags_list)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": "tag",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "tags": tags_list[:max_events],
        }

    # ── Galaxy search ──────────────────────────────────────────────────

    async def _search_galaxy(
        self,
        client: MISPClient,
        query: str,
        params: dict,
        scope: str,
    ) -> dict:
        """Search galaxies or galaxy clusters."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        galaxy_id = params.get("galaxy_id")
        cluster_id = params.get("cluster_id")

        result: dict | list = {}
        if scope == "galaxy":
            result = await client.search("galaxies", searchFor=query, limit=max_events)
        else:
            # galaxy_cluster
            if galaxy_id:
                result = await client.search(
                    "galaxy_clusters", galaxy_id=galaxy_id, searchFor=query, limit=max_events
                )
            else:
                result = await client.search("galaxy_clusters", searchFor=query, limit=max_events)

        raw_items = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        items: list[dict] = []
        for item in raw_items:
            if isinstance(item, dict):
                items.append({
                    "id": item.get("id"),
                    "uuid": item.get("uuid"),
                    "name": item.get("name", item.get("value", "")),
                    "type": item.get("type", ""),
                    "description": str(item.get("description", ""))[:300],
                    "version": item.get("version"),
                })

        total_results = len(items)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": scope,
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "items": items[:max_events],
        }

    # ── Feed search ────────────────────────────────────────────────────

    async def _search_feed(
        self,
        client: MISPClient,
        params: dict,
    ) -> dict:
        """List configured feeds."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        result = await client.get_feeds_list()
        raw_feeds = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        feeds: list[dict] = []
        for feed in raw_feeds:
            if isinstance(feed, dict):
                f = feed.get("Feed", feed)
                feeds.append({
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "provider": f.get("provider"),
                    "url": f.get("url"),
                    "enabled": f.get("enabled"),
                    "rules": f.get("rules"),
                })

        total_results = len(feeds)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "search_type": "feed",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "feeds": feeds[:max_events],
        }

    # ── Taxonomy search ────────────────────────────────────────────────

    async def _search_taxonomy(
        self,
        client: MISPClient,
        query: str,
    ) -> dict:
        """List/search taxonomies."""
        result = await client.get_taxonomies_list()
        raw = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        taxonomies: list[dict] = []
        for t in raw:
            if isinstance(t, dict):
                tax = t.get("Taxonomy", t)
                name = tax.get("namespace", tax.get("name", ""))
                if query and query.lower() not in name.lower():
                    continue
                taxonomies.append({
                    "id": tax.get("id"),
                    "namespace": tax.get("namespace"),
                    "description": str(tax.get("description", ""))[:200],
                    "enabled": tax.get("enabled"),
                    "version": tax.get("version"),
                    "exclusive": tax.get("exclusive"),
                    "tags_count": len(tax.get("entries", [])),
                })

        return {
            "query": query,
            "search_type": "taxonomy",
            "total_results": len(taxonomies),
            "total_pages": 1,
            "current_page": 1,
            "has_next": False,
            "truncated": False,
            "taxonomies": taxonomies,
        }

    # ── Warninglist search ─────────────────────────────────────────────

    async def _search_warninglist(
        self,
        client: MISPClient,
        query: str,
        params: dict,
    ) -> dict:
        """Search warninglists by name. If query is an IOC, check against them."""
        result = await client.get_warninglists()
        raw = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        warninglists: list[dict] = []
        ioc_hits: list[dict] = []

        for wl in raw:
            if not isinstance(wl, dict):
                continue
            w = wl.get("Warninglist", wl)
            name = w.get("name", "")
            if query and query.lower() not in name.lower():
                continue
            warninglists.append({
                "id": w.get("id"),
                "name": name,
                "description": str(w.get("description", ""))[:200],
                "enabled": w.get("enabled"),
                "version": w.get("version"),
                "type": w.get("type"),
            })

        # If query looks like an IOC, check it against warninglists
        if Q.detect_ioc_type(query):
            try:
                check_result = await client.search(
                    "warninglists", checkValue=query
                )
                if isinstance(check_result, dict):
                    hits = check_result.get("response", check_result.get("hits", []))
                    if isinstance(hits, list):
                        for hit in hits:
                            if isinstance(hit, dict):
                                ioc_hits.append({
                                    "warninglist_name": hit.get("name", ""),
                                    "matched": hit.get("matched", False),
                                })
            except MISPError:
                pass

        return {
            "query": query,
            "search_type": "warninglist",
            "total_results": len(warninglists),
            "total_pages": 1,
            "current_page": 1,
            "has_next": False,
            "truncated": False,
            "warninglists": warninglists,
            "ioc_hits": ioc_hits if ioc_hits else None,
        }

    # ── Sighting search ────────────────────────────────────────────────

    async def _search_sighting(
        self,
        client: MISPClient,
        query: str,
        params: dict,
    ) -> dict:
        """Search sightings by event, attribute, org, or date."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        search_kwargs: dict[str, Any] = {"limit": max_events}
        if query:
            search_kwargs["value"] = query

        result = await client.search("sightings", **search_kwargs)
        raw = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        sightings: list[dict] = []
        for s in raw:
            if isinstance(s, dict):
                sightings.append({
                    "id": s.get("id"),
                    "event_id": s.get("event_id"),
                    "attribute_id": s.get("attribute_id"),
                    "type": s.get("type"),
                    "source": s.get("source"),
                    "date_sighting": s.get("date_sighting"),
                    "org_id": s.get("org_id"),
                })

        total_results = len(sightings)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": "sighting",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "sightings": sightings[:max_events],
        }

    # ── Event Report search ────────────────────────────────────────────

    async def _search_event_report(
        self,
        client: MISPClient,
        query: str,
        params: dict,
    ) -> dict:
        """Search Event Reports by event or keyword."""
        max_events = int(params.get("max_events", Q.DEFAULT_MAX_EVENTS))
        search_kwargs: dict[str, Any] = {"limit": max_events}
        if query:
            search_kwargs["value"] = query

        result = await client.search("event_reports", **search_kwargs)
        raw = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        reports: list[dict] = []
        for r in raw:
            if isinstance(r, dict):
                reports.append({
                    "id": r.get("id"),
                    "uuid": r.get("uuid"),
                    "event_id": r.get("event_id"),
                    "name": r.get("name"),
                    "content": str(r.get("content", ""))[:500],
                    "timestamp": r.get("timestamp"),
                })

        total_results = len(reports)
        page = int(params.get("page", 1))
        total_pages = max(1, -(-total_results // max_events)) if max_events else 1

        return {
            "query": query,
            "search_type": "event_report",
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "has_next": page < total_pages,
            "truncated": total_results > max_events,
            "event_reports": reports[:max_events],
        }

    # ── Organisation search ────────────────────────────────────────────

    async def _search_organisation(
        self,
        client: MISPClient,
        query: str,
    ) -> dict:
        """Search organisations (local and remote)."""
        result = await client.search("organisations", searchFor=query or "", limit=50)
        raw = result if isinstance(result, list) else (
            result.get("response", []) if isinstance(result, dict) else []
        )

        orgs: list[dict] = []
        for org in raw:
            if isinstance(org, dict):
                o = org.get("Organisation", org)
                orgs.append({
                    "id": o.get("id"),
                    "uuid": o.get("uuid"),
                    "name": o.get("name"),
                    "local": o.get("local"),
                    "description": str(o.get("description", ""))[:200],
                    "nationality": o.get("nationality"),
                })

        return {
            "query": query,
            "search_type": "organisation",
            "total_results": len(orgs),
            "total_pages": 1,
            "current_page": 1,
            "has_next": False,
            "truncated": False,
            "organisations": orgs,
        }

    # ── Convenience helpers ────────────────────────────────────────────

    def _success(
        self,
        data: dict,
        execution_time_ms: int = 0,
    ) -> ToolResult:
        return ToolResult.success(
            data=data,
            tool_name=self.name,
            version=self.version,
            execution_time_ms=execution_time_ms,
        )

    def _failure(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        execution_time_ms: int = 0,
    ) -> ToolResult:
        return ToolResult.failure(
            code=code,
            message=message,
            retryable=retryable,
            tool_name=self.name,
            version=self.version,
            execution_time_ms=execution_time_ms,
        )
