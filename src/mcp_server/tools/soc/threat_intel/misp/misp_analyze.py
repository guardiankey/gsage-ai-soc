"""gSage AI — MISP Analyze tool.

Intelligence analysis over MISP data without modifying it. Centralises
functionality previously scattered across misp_search and misp_manage:
similarity scoring, event diff, explanation, tag suggestions, merge
suggestions, report generation and correlation graphs.

Required permission: ``misp:read``
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.threat_intel.misp import _analyze, _query as Q
from src.mcp_server.tools.soc.threat_intel.misp._client import MISPClient, MISPError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = [
    "similarity", "diff_events", "explain_event",
    "suggest_tags", "suggest_merge", "generate_report",
    "correlation_graph",
]


class MispAnalyzeTool(BaseTool):
    """Intelligence analysis over MISP data.

    Provides similarity scoring, event comparison, natural-language
    explanation, tag/merge suggestions, report generation and
    correlation graphs. Read-only — never modifies MISP data.

    Permission: ``misp:read``
    """

    name: ClassVar[str] = "misp_analyze"
    config_namespace: ClassVar[str] = "misp"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "MISP intelligence analysis: similarity, diff, explanation, "
        "tag suggestions, merge suggestions, report generation, graphs"
    )
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["misp:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
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
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": _ACTIONS,
                "description": "Analytical operation to perform.",
            },
            "event_id": {
                "type": "integer",
                "description": "Target event ID for similarity, explain_event, suggest_tags, generate_report, correlation_graph. For similarity, the tool extracts features from it and searches for similar events.",
            },
            "event_id_a": {
                "type": "integer",
                "description": "First event ID for diff_events.",
            },
            "event_id_b": {
                "type": "integer",
                "description": "Second event ID for diff_events.",
            },
            "ioc_value": {
                "type": "string",
                "description": "IOC for similarity: IP, domain, hash, etc. Alternative to event_id — use one or the other.",
            },
            "threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "default": 70,
                "description": "Minimum similarity threshold (similarity and suggest_merge).",
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
                "description": "Number of results (similarity, suggest_merge).",
            },
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "default": 90,
                "description": "Search window for suggest_merge.",
            },
            "strategy": {
                "type": "string",
                "enum": ["hybrid", "ioc_only", "attack_only", "tags_only"],
                "default": "hybrid",
                "description": "Similarity strategy (action=similarity).",
            },
            "template": {
                "type": "string",
                "enum": ["executive", "technical", "ioc_only"],
                "default": "executive",
                "description": "Template for generate_report: executive (managerial summary), technical (detailed with MITRE and galaxies), ioc_only (IOC table only).",
            },
            "graph_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "default": 2,
                "description": "Correlation graph depth.",
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
        action = str(params.get("action", "")).strip()

        if not action:
            return self._failure("VALIDATION_ERROR", "Parameter 'action' is required.", retryable=False)

        try:
            client = MISPClient(
                url=config["url"],
                api_key=config["api_key"],
                verify_cert=config.get("verify_cert", True),
            )

            if action == "similarity":
                result_data = await self._similarity(client, params)
            elif action == "diff_events":
                result_data = await self._diff_events(client, params)
            elif action == "explain_event":
                result_data = await self._explain_event(client, params)
            elif action == "suggest_tags":
                result_data = await self._suggest_tags(client, params)
            elif action == "suggest_merge":
                result_data = await self._suggest_merge(client, params)
            elif action == "generate_report":
                result_data = await self._generate_report(client, params)
            elif action == "correlation_graph":
                result_data = await self._correlation_graph(client, params)
            else:
                return self._failure("VALIDATION_ERROR", f"Unknown action: {action}", retryable=False)

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._success(result_data, execution_time_ms=elapsed_ms)

        except MISPError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, exc.message, retryable=Q.is_retryable_error(exc), execution_time_ms=elapsed_ms)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.exception("Unexpected error in misp_analyze")
            return self._failure("INTERNAL_ERROR", str(exc), retryable=False, execution_time_ms=elapsed_ms)

    # ── Similarity ─────────────────────────────────────────────────────

    async def _similarity(self, client: MISPClient, params: dict) -> dict:
        strategy = str(params.get("strategy", "hybrid"))
        threshold = float(params.get("threshold", 70))
        top_n = int(params.get("top_n", 5))

        # Determine source features
        event_id = params.get("event_id")
        ioc_value = params.get("ioc_value")

        if event_id:
            # Use event as source for features
            source_event = await client.get_event(event_id)
            source_normalized = Q.normalize_event(source_event)
            source_features = _analyze.extract_features(source_normalized)
            # Search for similar events
            search_query = " ".join(source_features.get("iocs", [])[:3] or [""])
            search_results = await client.search("events", eventinfo=search_query, metadata=True, limit=20)
        elif ioc_value:
            source_features = {"iocs": [ioc_value], "attack_techniques": [], "galaxies": [], "tags": []}
            search_results = await client.search("attributes", value=ioc_value, limit=20, includeEventTags=True)
        else:
            raise MISPError("Either 'event_id' or 'ioc_value' is required for similarity", code="VALIDATION_ERROR")

        # Normalize target events
        raw_events = search_results if isinstance(search_results, list) else (
            search_results.get("response", search_results.get("Event", []))
            if isinstance(search_results, dict) else []
        )

        similarities: list[dict] = []
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                continue
            normalized = Q.normalize_event(raw_event)
            target_features = _analyze.extract_features(normalized)
            score = _analyze.compute_similarity(source_features, target_features, strategy)

            if score >= threshold:
                similarities.append({
                    "event_id": normalized.get("id"),
                    "title": normalized.get("title"),
                    "score": score,
                    "shared_iocs": list(
                        set(source_features.get("iocs", [])) & set(target_features.get("iocs", []))
                    )[:10],
                    "shared_attack": list(
                        set(source_features.get("attack_techniques", [])) & set(target_features.get("attack_techniques", []))
                    ),
                    "shared_galaxies": list(
                        set(source_features.get("galaxies", [])) & set(target_features.get("galaxies", []))
                    )[:10],
                })

        similarities.sort(key=lambda x: x["score"], reverse=True)

        return {
            "action": "similarity",
            "strategy": strategy,
            "threshold": threshold,
            "source": {"event_id": event_id, "ioc_value": ioc_value},
            "total_results": len(similarities),
            "results": similarities[:top_n],
        }

    # ── Diff events ────────────────────────────────────────────────────

    async def _diff_events(self, client: MISPClient, params: dict) -> dict:
        event_id_a = params.get("event_id_a")
        event_id_b = params.get("event_id_b")
        if not event_id_a or not event_id_b:
            raise MISPError("'event_id_a' and 'event_id_b' are required", code="VALIDATION_ERROR")

        event_a = await client.get_event(event_id_a)
        event_b = await client.get_event(event_id_b)

        norm_a = Q.normalize_event(event_a)
        norm_b = Q.normalize_event(event_b)

        # IOCs
        iocs_a = {a["value"] for a in norm_a.get("attributes", [])}
        iocs_b = {a["value"] for a in norm_b.get("attributes", [])}

        # ATT&CK
        attack_a = set(norm_a.get("attack_techniques", []))
        attack_b = set(norm_b.get("attack_techniques", []))

        # Galaxies
        galaxies_a = {f"{g.get('name', '')}:{g.get('cluster', '')}".strip(":") for g in norm_a.get("galaxies", [])}
        galaxies_b = {f"{g.get('name', '')}:{g.get('cluster', '')}".strip(":") for g in norm_b.get("galaxies", [])}

        # Tags
        tags_a = set(norm_a.get("tags", []))
        tags_b = set(norm_b.get("tags", []))

        return {
            "action": "diff_events",
            "event_a": {"id": event_id_a, "title": norm_a.get("title"), "date": norm_a.get("date")},
            "event_b": {"id": event_id_b, "title": norm_b.get("title"), "date": norm_b.get("date")},
            "common_attributes": sorted(iocs_a & iocs_b),
            "new_attributes": sorted(iocs_b - iocs_a),
            "removed_attributes": sorted(iocs_a - iocs_b),
            "common_attack": sorted(attack_a & attack_b),
            "new_attack": sorted(attack_b - attack_a),
            "removed_attack": sorted(attack_a - attack_b),
            "common_galaxies": sorted(galaxies_a & galaxies_b),
            "new_galaxies": sorted(galaxies_b - galaxies_a),
            "removed_galaxies": sorted(galaxies_a - galaxies_b),
            "common_tags": sorted(tags_a & tags_b),
            "new_tags": sorted(tags_b - tags_a),
            "removed_tags": sorted(tags_a - tags_b),
            "summary": (
                f"Event #{event_id_a} ('{norm_a.get('title')}') and "
                f"Event #{event_id_b} ('{norm_b.get('title')}') share "
                f"{len(iocs_a & iocs_b)} IOCs and "
                f"{len(attack_a & attack_b)} ATT&CK techniques."
            ),
        }

    # ── Explain event ──────────────────────────────────────────────────

    async def _explain_event(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required", code="VALIDATION_ERROR")

        event = await client.get_event(event_id)
        normalized = Q.normalize_event(event)

        # Build narrative
        title = normalized.get("title", "Unknown")
        date = normalized.get("date", "unknown date")
        org = normalized.get("org", {}).get("name", "Unknown org")
        tl_map = {1: "High", 2: "Medium", 3: "Low", 4: "Undefined"}
        threat_level = tl_map.get(normalized.get("threat_level_id", 4), "Undefined")
        published = "published" if normalized.get("published") else "unpublished"

        # Summarise IOCs by type
        ioc_summary: dict[str, int] = {}
        for attr in normalized.get("attributes", []):
            atype = attr.get("type", "unknown")
            ioc_summary[atype] = ioc_summary.get(atype, 0) + 1

        ioc_summary_str = ", ".join(f"{v}× {k}" for k, v in sorted(ioc_summary.items()))

        narrative = (
            f"Event #{event_id}: '{title}' — reported by {org} on {date}. "
            f"Threat level: {threat_level}. Status: {published}. "
            f"Contains {normalized.get('attribute_count', 0)} attributes ({ioc_summary_str}). "
        )

        if normalized.get("attack_techniques"):
            narrative += (
                f"MITRE ATT&CK: {', '.join(normalized['attack_techniques'][:5])}"
                f"{'...' if len(normalized['attack_techniques']) > 5 else ''}. "
            )
        if normalized.get("galaxies"):
            actors = [g["cluster"] for g in normalized["galaxies"] if "actor" in (g.get("name", "")).lower()]
            malware = [g["cluster"] for g in normalized["galaxies"] if "malware" in (g.get("name", "")).lower()]
            if actors:
                narrative += f"Threat Actors: {', '.join(actors[:3])}. "
            if malware:
                narrative += f"Malware: {', '.join(malware[:3])}. "

        if normalized.get("tags"):
            narrative += f"Tags: {', '.join(normalized['tags'][:10])}. "

        return {
            "action": "explain_event",
            "event_id": event_id,
            "title": title,
            "date": date,
            "organisation": org,
            "threat_level": threat_level,
            "published": normalized.get("published"),
            "uuid": normalized.get("uuid"),
            "narrative": narrative,
            "ioc_summary": ioc_summary,
            "attack_techniques": normalized.get("attack_techniques", [])[:20],
            "galaxies": normalized.get("galaxies", [])[:10],
            "tags": normalized.get("tags", [])[:20],
        }

    # ── Suggest tags ───────────────────────────────────────────────────

    async def _suggest_tags(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            raise MISPError("'event_id' is required", code="VALIDATION_ERROR")

        event = await client.get_event(event_id)
        normalized = Q.normalize_event(event)

        suggested_tags: list[str] = []
        suggested_galaxies: list[dict] = []
        suggested_attack: list[str] = []

        # Heuristic tag suggestions based on IOCs
        has_url = any(a["type"] == "url" for a in normalized.get("attributes", []))
        has_email = any(a["type"] in ("email-src", "email") for a in normalized.get("attributes", []))
        has_ip = any(a["type"] in ("ip-src", "ip-dst") for a in normalized.get("attributes", []))
        has_hash = any(a["type"] in ("md5", "sha1", "sha256", "sha512") for a in normalized.get("attributes", []))
        has_domain = any(a["type"] == "domain" for a in normalized.get("attributes", []))

        if has_url and has_email:
            suggested_tags.append("phishing")
            suggested_attack.append("T1566")  # Phishing
        if has_ip and has_domain:
            suggested_tags.append("c2")
            suggested_attack.append("T1105")  # Ingress Tool Transfer
        if has_hash:
            suggested_tags.append("malware")
        if has_email:
            suggested_tags.append("email")

        # TLP suggestion based on content
        if has_hash or normalized.get("threat_level_id") == 1:
            suggested_tags.append("tlp:amber")

        return {
            "action": "suggest_tags",
            "event_id": event_id,
            "suggested_tags": suggested_tags,
            "suggested_galaxies": suggested_galaxies,
            "suggested_attack_techniques": suggested_attack,
            "note": "These are suggestions only — apply via misp_manage(action='add_tag', ...).",
        }

    # ── Suggest merge ──────────────────────────────────────────────────

    async def _suggest_merge(self, client: MISPClient, params: dict) -> dict:
        threshold = float(params.get("threshold", 70))
        top_n = int(params.get("top_n", 5))
        window_days = int(params.get("window_days", 90))

        import datetime as dt_mod
        date_from = (dt_mod.datetime.now(dt_mod.timezone.utc) - dt_mod.timedelta(days=window_days)).strftime("%Y-%m-%d")

        # Fetch recent events
        result = await client.search("events", date_from=date_from, metadata=True, limit=50)
        raw = result if isinstance(result, list) else result.get("response", result.get("Event", []))

        events: list[dict] = []
        for e in raw:
            if isinstance(e, dict):
                events.append(Q.normalize_event(e))

        # Compare all pairs
        pairs: list[dict] = []
        seen_pairs: set[tuple] = set()

        for i, evt_a in enumerate(events):
            for j, evt_b in enumerate(events):
                if i >= j:
                    continue
                id_a = evt_a.get("id") or 0
                id_b = evt_b.get("id") or 0
                pair_key = (min(id_a, id_b), max(id_a, id_b))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                feat_a = _analyze.extract_features(evt_a)
                feat_b = _analyze.extract_features(evt_b)
                score = _analyze.compute_similarity(feat_a, feat_b)

                if score >= threshold:
                    pairs.append({
                        "event_a_id": evt_a.get("id"),
                        "event_b_id": evt_b.get("id"),
                        "event_a_title": evt_a.get("title"),
                        "event_b_title": evt_b.get("title"),
                        "score": score,
                        "recommendation": "merge_events",
                    })

        pairs.sort(key=lambda x: x["score"], reverse=True)

        return {
            "action": "suggest_merge",
            "window_days": window_days,
            "threshold": threshold,
            "total_events_scanned": len(events),
            "total_pairs_found": len(pairs),
            "candidates": pairs[:top_n],
        }

    # ── Generate report ────────────────────────────────────────────────

    async def _generate_report(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        template = str(params.get("template", "executive"))
        if not event_id:
            raise MISPError("'event_id' is required", code="VALIDATION_ERROR")

        event = await client.get_event(event_id)
        normalized = Q.normalize_event(event)

        if template == "ioc_only":
            # Simple IOC table
            ioc_table = "| Type | Value | Category | IDS |\n|------|-------|----------|-----|\n"
            for attr in normalized.get("attributes", [])[:50]:
                ioc_table += (
                    f"| {attr.get('type')} | {attr.get('value')} | "
                    f"{attr.get('category')} | {'✓' if attr.get('to_ids') else ''} |\n"
                )
            report = ioc_table
        elif template == "technical":
            report = _build_technical_report(normalized)
        else:  # executive
            report = _build_executive_report(normalized)

        return {
            "action": "generate_report",
            "event_id": event_id,
            "template": template,
            "title": normalized.get("title"),
            "report": report,
            "ready_for": "misp_manage(action='create_event_report', event_report_content=...).",
        }

    # ── Correlation graph ──────────────────────────────────────────────

    async def _correlation_graph(self, client: MISPClient, params: dict) -> dict:
        event_id = params.get("event_id")
        depth = int(params.get("graph_depth", 2))
        if not event_id:
            raise MISPError("'event_id' is required", code="VALIDATION_ERROR")

        event = await client.get_event(event_id)
        normalized = Q.normalize_event(event)
        graph = _analyze.build_correlation_graph([normalized], depth=depth)

        return {
            "action": "correlation_graph",
            "event_id": event_id,
            "depth": depth,
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
            "graph": graph,
        }

    # ── Convenience helpers ────────────────────────────────────────────

    def _success(self, data: dict, execution_time_ms: int = 0) -> ToolResult:
        return ToolResult.success(
            data=data, tool_name=self.name, version=self.version,
            execution_time_ms=execution_time_ms,
        )

    def _failure(self, code: str, message: str, retryable: bool = False, execution_time_ms: int = 0) -> ToolResult:
        return ToolResult.failure(
            code=code, message=message, retryable=retryable,
            tool_name=self.name, version=self.version,
            execution_time_ms=execution_time_ms,
        )


# ── Report builders ───────────────────────────────────────────────────────

def _build_executive_report(event: dict) -> str:
    """Build an executive summary report."""
    title = event.get("title", "Unknown")
    date = event.get("date", "unknown")
    org = event.get("org", {}).get("name", "Unknown")
    tl_map = {1: "High", 2: "Medium", 3: "Low", 4: "Undefined"}
    tl = tl_map.get(event.get("threat_level_id", 4), "Undefined")

    lines = [
        f"# {title}",
        f"",
        f"**Date**: {date} | **Organisation**: {org} | **Threat Level**: {tl}",
        f"",
        f"## Executive Summary",
        f"",
        f"This event contains {event.get('attribute_count', 0)} IOCs across "
        f"{len(event.get('attack_techniques', []))} MITRE ATT&CK techniques.",
        f"",
    ]

    if event.get("attack_techniques"):
        lines.append("## MITRE ATT&CK")
        for tech in event["attack_techniques"][:10]:
            lines.append(f"- {tech}")
        lines.append("")

    if event.get("galaxies"):
        lines.append("## Threat Intelligence")
        for galaxy in event["galaxies"][:5]:
            lines.append(f"- **{galaxy.get('name')}**: {galaxy.get('cluster')}")
        lines.append("")

    lines.append("## IOCs")
    lines.append("| Type | Value | IDS |")
    lines.append("|------|-------|-----|")
    for attr in event.get("attributes", [])[:20]:
        lines.append(f"| {attr.get('type')} | {attr.get('value')} | {'✓' if attr.get('to_ids') else ''} |")

    return "\n".join(lines)


def _build_technical_report(event: dict) -> str:
    """Build a detailed technical report."""
    lines = [_build_executive_report(event)]

    if event.get("tags"):
        lines.append("")
        lines.append("## Tags")
        for tag in event.get("tags", [])[:20]:
            lines.append(f"- {tag}")

    if event.get("galaxies"):
        lines.append("")
        lines.append("## Galaxies")
        for galaxy in event.get("galaxies", []):
            lines.append(f"- **{galaxy.get('name')}**: {galaxy.get('cluster')}")
            if galaxy.get("description"):
                lines.append(f"  {galaxy['description'][:200]}")

    lines.append("")
    lines.append("## All IOCs")
    lines.append("| Type | Value | Category | IDS | Comment |")
    lines.append("|------|-------|----------|-----|---------|")
    for attr in event.get("attributes", []):
        lines.append(
            f"| {attr.get('type')} | {attr.get('value')} | "
            f"{attr.get('category')} | {'✓' if attr.get('to_ids') else ''} | "
            f"{attr.get('comment', '')} |"
        )

    return "\n".join(lines)
