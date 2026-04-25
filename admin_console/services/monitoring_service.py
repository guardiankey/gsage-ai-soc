"""Admin Console — monitoring service: health checks for all services."""

from __future__ import annotations

import asyncio
from typing import Any


# ─── Individual checks ────────────────────────────────────────────────────────

async def check_postgres() -> dict[str, Any]:
    try:
        from admin_console.db.postgres import get_session  # noqa: PLC0415
        from sqlalchemy import text  # noqa: PLC0415

        async with get_session() as db:
            result = await db.execute(text("SELECT 1"))
            result.fetchone()
        return {"status": "ok", "details": "Connection successful"}
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def check_redis() -> dict[str, Any]:
    try:
        from admin_console.config import get_admin_settings  # noqa: PLC0415
        from admin_console.db.docker_ops import docker_redis_cli  # noqa: PLC0415

        s = get_admin_settings()
        rc, out = await asyncio.to_thread(
            docker_redis_cli, "PING", s.redis_password or ""
        )
        # redis-cli with -a prints a warning to stderr; look for PONG in output
        if rc == 0 and "PONG" in out:
            # Also get version for detail
            _, info_out = await asyncio.to_thread(
                docker_redis_cli, "INFO server", s.redis_password or ""
            )
            version = "?"
            for line in info_out.splitlines():
                if line.startswith("redis_version:"):
                    version = line.split(":", 1)[1].strip()
                    break
            return {"status": "ok", "details": f"Redis {version}"}
        return {"status": "error", "details": out[:120] or "No response"}
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def check_elasticsearch() -> dict[str, Any]:
    try:
        from admin_console.db.es_client import es_health  # noqa: PLC0415

        health = await asyncio.to_thread(es_health)
        if "error" in health:
            return {"status": "error", "details": health["error"]}
        es_status = health.get("status", "unknown")
        return {
            "status": "ok" if es_status in ("green", "yellow") else "error",
            "details": f"cluster_status={es_status}",
        }
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def check_weaviate() -> dict[str, Any]:
    try:
        from admin_console.db.weaviate_ops import weaviate_collections  # noqa: PLC0415

        cols = await weaviate_collections()
        return {"status": "ok", "details": f"{len(cols)} collections"}
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def check_minio() -> dict[str, Any]:
    try:
        from admin_console.db.docker_ops import _run  # noqa: PLC0415

        # MinIO image includes curl; health endpoint returns 200 when ready
        rc, out = await asyncio.to_thread(
            _run,
            ["docker", "exec", "gsage-minio", "curl", "-sf",
             "http://localhost:9000/minio/health/live"],
        )
        if rc == 0:
            return {"status": "ok", "details": "MinIO live"}
        return {"status": "error", "details": out[:120] or "Health check failed"}
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def check_docker() -> dict[str, Any]:
    try:
        from admin_console.db.docker_ops import docker_ps  # noqa: PLC0415

        containers = await asyncio.to_thread(docker_ps)
        running = sum(1 for c in containers if c.state == "running")
        total = len(containers)
        return {
            "status": "ok" if running > 0 else "warning",
            "details": f"{running}/{total} running",
            "containers": containers,
        }
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def _check_container(container_name: str) -> dict[str, Any]:
    """Check if a named Docker container is running."""
    try:
        from admin_console.db.docker_ops import _run  # noqa: PLC0415

        rc, out = await asyncio.to_thread(
            _run,
            ["docker", "inspect", "--format={{.State.Status}}", container_name],
        )
        if rc != 0:
            return {"status": "error", "details": "not found"}
        state = out.strip()
        return {
            "status": "ok" if state == "running" else "error",
            "details": state,
        }
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def check_backend_api() -> dict[str, Any]:
    result = await _check_container("gsage-backend_api")
    if result["status"] == "ok":
        try:
            from admin_console.db.docker_ops import _run  # noqa: PLC0415

            rc, out = await asyncio.to_thread(
                _run,
                ["docker", "exec", "gsage-backend_api", "curl", "-sf",
                 "http://localhost:8000/health"],
            )
            if rc == 0:
                result["details"] = "running + /health ok"
        except Exception:
            pass
    return result


async def check_mcp_server() -> dict[str, Any]:
    return await _check_container("gsage-mcp-server")


async def check_celery_workers() -> dict[str, Any]:
    try:
        from admin_console.db.docker_ops import docker_ps  # noqa: PLC0415

        containers = await asyncio.to_thread(docker_ps)
        workers = [c for c in containers if c.name.startswith("gsage-celery-") and c.name != "gsage-celery-beat"]
        running = sum(1 for c in workers if c.state == "running")
        total = len(workers)
        return {
            "status": "ok" if running > 0 else "error",
            "details": f"{running}/{total} workers",
        }
    except Exception as exc:
        return {"status": "error", "details": str(exc)}


async def check_celery_beat() -> dict[str, Any]:
    return await _check_container("gsage-celery-beat")


async def check_ollama() -> dict[str, Any]:
    result = await _check_container("gsage-ollama")
    if result["status"] == "ok":
        try:
            from admin_console.db.docker_ops import _run  # noqa: PLC0415

            rc, out = await asyncio.to_thread(
                _run,
                ["docker", "exec", "gsage-ollama", "curl", "-sf",
                 "http://localhost:11434/api/version"],
            )
            if rc == 0:
                result["details"] = "running + API ok"
        except Exception:
            pass
    return result


async def check_frontend() -> dict[str, Any]:
    return await _check_container("gsage-frontend")


async def check_email_worker() -> dict[str, Any]:
    return await _check_container("gsage-email-worker")


# ─── Full health sweep ─────────────────────────────────────────────────────────

async def get_full_health() -> dict[str, dict[str, Any]]:
    results = await asyncio.gather(
        check_postgres(),
        check_redis(),
        check_elasticsearch(),
        check_weaviate(),
        check_minio(),
        check_docker(),
        check_backend_api(),
        check_mcp_server(),
        check_celery_workers(),
        check_celery_beat(),
        check_ollama(),
        check_frontend(),
        check_email_worker(),
        return_exceptions=False,
    )
    return {
        "postgres": results[0],
        "redis": results[1],
        "elasticsearch": results[2],
        "weaviate": results[3],
        "minio": results[4],
        "docker": results[5],
        "backend_api": results[6],
        "mcp_server": results[7],
        "celery_workers": results[8],
        "celery_beat": results[9],
        "ollama": results[10],
        "frontend": results[11],
        "email_worker": results[12],
    }
