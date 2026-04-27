"""Admin Console — Weaviate async client wrapper (Weaviate v4 API)."""

from __future__ import annotations

from typing import Any, Optional


async def _client():
    """Return the shared async Weaviate client (already connected)."""
    from src.shared.weaviate_client import get_weaviate_client  # noqa: PLC0415

    return await get_weaviate_client()


async def weaviate_collections() -> list[str]:
    """Return list of Weaviate collection names. Returns [] on error."""
    try:
        client = await _client()
        all_cols = await client.collections.list_all()
        return list(all_cols.keys())
    except Exception:
        return []


async def weaviate_count(collection: str) -> int:
    """Return document count for a collection (v4 aggregate API)."""
    try:
        from weaviate.classes.aggregate import GroupByAggregate  # noqa: PLC0415

        client = await _client()
        col = client.collections.get(collection)
        result = await col.aggregate.over_all(total_count=True)
        return result.total_count or 0
    except Exception:
        return -1


async def weaviate_search(
    collection: str,
    query: str,
    limit: int = 20,
    org_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """BM25 search inside a Weaviate collection (v4 API)."""
    try:
        from weaviate.classes.query import Filter  # noqa: PLC0415

        client = await _client()
        col = client.collections.get(collection)
        filters = Filter.by_property("org_id").equal(org_id) if org_id else None
        response = await col.query.bm25(
            query=query,
            limit=limit,
            filters=filters,
        )
        results: list[dict[str, Any]] = []
        for obj in response.objects:
            row = dict(obj.properties)
            row["_uuid"] = str(obj.uuid)
            results.append(row)
        return results
    except Exception as exc:
        return [{"error": str(exc)}]


async def weaviate_delete_collection(collection: str) -> tuple[bool, str]:
    """Delete an entire Weaviate collection."""
    try:
        client = await _client()
        await client.collections.delete(collection)
        return True, f"Deleted collection '{collection}'"
    except Exception as exc:
        return False, str(exc)


async def weaviate_delete_object(collection: str, uuid: str) -> tuple[bool, str]:
    """Delete a single object by UUID (v4 API)."""
    try:
        import uuid as _uuid_mod  # noqa: PLC0415

        client = await _client()
        col = client.collections.get(collection)
        await col.data.delete_by_id(_uuid_mod.UUID(uuid))
        return True, f"Deleted object {uuid}"
    except Exception as exc:
        return False, str(exc)
