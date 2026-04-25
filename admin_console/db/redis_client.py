"""Admin Console — Redis client wrapper via docker exec.

All functions execute ``redis-cli`` inside the ``gsage-redis`` container,
avoiding the need to expose Redis port to the host.
All functions are synchronous and intended for Textual worker threads.
"""

from __future__ import annotations

from typing import Any, Optional


def _redis_cli(args: str, db: int = 0) -> tuple[int, str]:
    """Run a redis-cli command inside gsage-redis; return (rc, output)."""
    from admin_console.config import get_admin_settings  # noqa: PLC0415
    from admin_console.db.docker_ops import docker_redis_cli  # noqa: PLC0415

    s = get_admin_settings()
    return docker_redis_cli(args, password=s.redis_password or "", db=db)


def redis_info(db: int = 0) -> dict[str, Any]:
    """Return Redis INFO as a flat dict."""
    try:
        rc, out = _redis_cli("INFO", db)
        if rc != 0:
            return {"error": out}
        result: dict[str, Any] = {}
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip()
        return result
    except Exception as exc:
        return {"error": str(exc)}


def redis_dbsize(db: int = 0) -> int:
    """Return number of keys in DB."""
    try:
        rc, out = _redis_cli("DBSIZE", db)
        if rc != 0:
            return -1
        # Output may contain a warning line; integer is the last non-empty number
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.isdigit():
                return int(line)
        return -1
    except Exception:
        return -1


def redis_scan_keys(pattern: str = "*", count: int = 100, db: int = 0) -> list[str]:
    """Scan for keys matching *pattern* (single SCAN call, safe for admin browse)."""
    try:
        rc, out = _redis_cli(f'SCAN 0 MATCH "{pattern}" COUNT {count}', db)
        if rc != 0:
            return []
        lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Warning")]
        # First non-empty line is cursor, rest are keys
        return sorted(lines[1:]) if len(lines) > 1 else []
    except Exception:
        return []


def redis_get(key: str, db: int = 0) -> Optional[str]:
    """Return a Redis key value as string."""
    try:
        rc, out = _redis_cli(f'GET "{key}"', db)
        if rc != 0:
            return None
        lines = [l for l in out.splitlines() if l.strip() and not l.startswith("Warning")]
        return lines[0] if lines else None
    except Exception:
        return None


def redis_delete(key: str, db: int = 0) -> bool:
    """Delete a Redis key; returns True on success."""
    try:
        rc, _ = _redis_cli(f'DEL "{key}"', db)
        return rc == 0
    except Exception:
        return False


def redis_flush_db(db: int = 0) -> bool:
    """Flush all keys in a single Redis DB (not all DBs)."""
    try:
        rc, _ = _redis_cli("FLUSHDB", db)
        return rc == 0
    except Exception:
        return False


def redis_queue_lengths() -> dict[str, int]:
    """Return Celery queue sizes from Redis DB 1."""
    queues = ["celery", "celery:0", "celery:1", "celery:2", "gsage"]
    result: dict[str, int] = {}
    for q in queues:
        try:
            rc, out = _redis_cli(f"LLEN {q}", db=1)
            if rc != 0:
                continue
            lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Warning")]
            if lines and lines[-1].isdigit():
                length = int(lines[-1])
                if length > 0:
                    result[q] = length
        except Exception:
            pass
    return result


def redbeat_keys() -> list[str]:
    """Return RedBeat schedule keys from Redis DB 1."""
    try:
        rc, out = _redis_cli('KEYS "redbeat:*"', db=1)
        if rc != 0:
            return []
        return sorted(
            l.strip() for l in out.splitlines()
            if l.strip() and not l.startswith("Warning")
        )
    except Exception:
        return []


def redis_key_type(key: str, db: int = 0) -> str:
    """Return the TYPE of a Redis key (string/list/hash/set/zset/?)."""
    try:
        rc, out = _redis_cli(f'TYPE "{key}"', db)
        if rc != 0:
            return "?"
        lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Warning")]
        return lines[0] if lines else "?"
    except Exception:
        return "?"


def redis_key_ttl(key: str, db: int = 0) -> str:
    """Return the TTL (seconds) of a Redis key as a string (-1 = no expiry)."""
    try:
        rc, out = _redis_cli(f'TTL "{key}"', db)
        if rc != 0:
            return "?"
        lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Warning")]
        return lines[-1] if lines else "?"
    except Exception:
        return "?"
