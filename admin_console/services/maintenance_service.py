"""Admin Console — maintenance service: cache flush, ES/Weaviate ops."""

from __future__ import annotations

from typing import Any


async def flush_cache(pattern: str = "*", db_index: int = 0) -> tuple[int, str]:
    """Delete all keys matching *pattern* from Redis db *db_index*.

    Returns (count_deleted, error_or_empty).
    """
    import asyncio  # noqa: PLC0415

    from admin_console.db.redis_client import redis_delete, redis_scan_keys  # noqa: PLC0415

    try:
        keys = await asyncio.to_thread(redis_scan_keys, pattern, 500, db_index)
        count = 0
        for k in keys:
            if await asyncio.to_thread(redis_delete, k, db_index):
                count += 1
        return count, ""
    except Exception as exc:
        return 0, str(exc)


async def flush_permissions_cache() -> tuple[int, str]:
    return await flush_cache(pattern="perm:*", db_index=0)


async def flush_apikeys_cache() -> tuple[int, str]:
    return await flush_cache(pattern="apikey:*", db_index=0)


async def flush_all_cache(db_index: int = 0) -> tuple[bool, str]:
    from admin_console.db.redis_client import redis_flush_db  # noqa: PLC0415

    ok = redis_flush_db(db=db_index)
    return ok, ""


async def delete_es_index(index_name: str) -> tuple[bool, str]:
    from admin_console.db.es_client import es_delete_index  # noqa: PLC0415

    return es_delete_index(index_name)


async def weaviate_cleanup(collection: str) -> tuple[bool, str]:
    from admin_console.db.weaviate_ops import weaviate_delete_collection  # noqa: PLC0415

    return await weaviate_delete_collection(collection)


async def weaviate_list_collections_with_counts() -> list[dict[str, Any]]:
    from admin_console.db.weaviate_ops import (  # noqa: PLC0415
        weaviate_collections,
        weaviate_count,
    )

    cols = await weaviate_collections()
    result = []
    for col_name in cols:
        count = await weaviate_count(col_name)
        result.append({"name": col_name, "count": count})
    return result


def get_db_size(settings: Any) -> dict[str, Any]:
    """Return PostgreSQL database size statistics."""
    import psycopg2  # noqa: PLC0415

    try:
        dsn = (
            f"host={settings.postgres_host} port={settings.postgres_port} "
            f"dbname={settings.postgres_database} "
            f"user={settings.postgres_username} "
            f"password={settings.postgres_password}"
        )
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_database.datname, pg_size_pretty(pg_database_size("
                    "pg_database.datname)) AS size FROM pg_database WHERE datname = current_database()"
                )
                row = cur.fetchone()
                return {"database": row[0], "size": row[1]} if row else {}
    except Exception as exc:
        return {"error": str(exc)}
