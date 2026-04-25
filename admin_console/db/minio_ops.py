"""Admin Console — MinIO sync client wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


def _get_client():
    from minio import Minio  # type: ignore[import]
    from admin_console.config import get_admin_settings

    s = get_admin_settings()
    # Use admin override endpoint when set (e.g. localhost:9000) so the
    # admin console can reach MinIO without requiring internal hostname resolution.
    endpoint = getattr(s, "admin_minio_endpoint", "") or s.minio_endpoint
    return Minio(
        endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=s.minio_secure,
    )


def minio_list_buckets() -> list[str]:
    """Return list of bucket names."""
    try:
        client = _get_client()
        return [b.name for b in client.list_buckets()]
    except Exception as exc:
        return ["error:" + str(exc)]


def minio_list_objects(
    bucket: str,
    prefix: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return list of objects in a bucket with metadata."""
    try:
        client = _get_client()
        objects = client.list_objects(bucket, prefix=prefix, recursive=True)
        result = []
        for i, obj in enumerate(objects):
            if i >= limit:
                break
            result.append({
                "name": obj.object_name,
                "size": obj.size,
                "last_modified": (
                    obj.last_modified.isoformat() if obj.last_modified else ""
                ),
                "content_type": getattr(obj, "content_type", ""),
            })
        return result
    except Exception as exc:
        return [{"error": str(exc)}]


def minio_stat_object(bucket: str, name: str) -> dict[str, Any]:
    """Return object metadata."""
    try:
        client = _get_client()
        stat = client.stat_object(bucket, name)
        return {
            "name": stat.object_name,
            "size": stat.size,
            "content_type": stat.content_type,
            "last_modified": (
                stat.last_modified.isoformat() if stat.last_modified else ""
            ),
            "metadata": dict(stat.metadata or {}),
        }
    except Exception as exc:
        return {"error": str(exc)}


def minio_presigned_url(
    bucket: str,
    name: str,
    expiry_seconds: int = 3600,
) -> str:
    """Return a presigned GET URL for the object."""
    try:
        from datetime import timedelta

        client = _get_client()
        return client.presigned_get_object(
            bucket, name, expires=timedelta(seconds=expiry_seconds)
        )
    except Exception as exc:
        return f"error:{exc}"


def minio_bucket_stats(bucket: str) -> dict[str, Any]:
    """Return aggregate stats for a bucket."""
    try:
        client = _get_client()
        total_size = 0
        count = 0
        for obj in client.list_objects(bucket, recursive=True):
            total_size += obj.size or 0
            count += 1
        return {"object_count": count, "total_size_bytes": total_size}
    except Exception as exc:
        return {"error": str(exc)}
