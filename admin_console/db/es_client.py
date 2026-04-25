"""Admin Console — Elasticsearch sync client wrapper."""

from __future__ import annotations

from typing import Any, Optional


def _get_client():
    from elasticsearch import Elasticsearch  # type: ignore[import]
    from admin_console.config import get_admin_settings

    s = get_admin_settings()
    return Elasticsearch(s.elasticsearch_url, request_timeout=10)


def es_health() -> dict[str, Any]:
    """Return cluster health."""
    try:
        return dict(_get_client().cluster.health())  # type: ignore[arg-type]
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)}


def es_indices() -> list[dict[str, Any]]:
    """Return list of indices with stats."""
    try:
        client = _get_client()
        raw = client.cat.indices(format="json", bytes="mb", v=True)
        return [dict(idx) for idx in raw]  # type: ignore[arg-type]
    except Exception as exc:
        return [{"error": str(exc)}]


def es_index_templates() -> list[str]:
    """Return list of index template names."""
    try:
        result = _get_client().indices.get_index_template()
        templates = result.get("index_templates", [])
        return [t["name"] for t in templates]
    except Exception:
        return []


def es_search_traces(
    query: str,
    index_prefix: str,
    size: int = 50,
    org_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Search agent trace documents."""
    try:
        client = _get_client()
        must: list[dict] = []
        if query:
            must.append({"match": {"_all": query}})
        if org_id:
            must.append({"term": {"org_id": org_id}})

        body: dict = {
            "query": {"bool": {"must": must}} if must else {"match_all": {}},
            "sort": [{"@timestamp": {"order": "desc"}}],
            "size": size,
        }
        result = client.search(index=f"{index_prefix}*", body=body)
        hits = result.get("hits", {}).get("hits", [])
        return [{"_id": h["_id"], **h.get("_source", {})} for h in hits]
    except Exception as exc:
        return [{"error": str(exc)}]


def es_delete_index(index: str) -> tuple[bool, str]:
    """Delete an Elasticsearch index."""
    try:
        _get_client().indices.delete(index=index)
        return True, f"Deleted index '{index}'"
    except Exception as exc:
        return False, str(exc)


def es_count(index_prefix: str) -> int:
    """Count documents in indices matching prefix."""
    try:
        result = _get_client().count(index=f"{index_prefix}*")
        return int(result.get("count", 0))
    except Exception:
        return -1


def es_recent_agent_runs(
    org_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the most recent agent runs for an org from Elasticsearch."""
    try:
        body: dict = {
            "query": {"term": {"org_id": org_id}},
            "sort": [{"@timestamp": {"order": "desc"}}],
            "size": limit,
            "_source": [
                "@timestamp",
                "agent_type",
                "maker_model",
                "reviewer_model",
                "status",
                "total_tokens",
                "elapsed_seconds",
                "has_error",
                "error_message",
            ],
        }
        result = _get_client().search(
            index="gsage-agent-runs-*",
            body=body,
        )
        hits = result.get("hits", {}).get("hits", [])
        return [{"_id": h["_id"], **h.get("_source", {})} for h in hits]
    except Exception as exc:
        return [{"error": str(exc)}]
