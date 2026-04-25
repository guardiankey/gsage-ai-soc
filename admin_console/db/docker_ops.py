"""Admin Console — Docker operations via subprocess.

Wraps ``docker compose`` commands.  All functions are synchronous
and intended to be called from a Textual worker thread.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Container:
    name: str
    image: str
    status: str
    ports: str
    state: str  # "running" | "exited" | "restarting" | ...


def _run(args: list[str], cwd: Optional[str] = None) -> tuple[int, str]:
    """Run a subprocess command; return (returncode, combined output)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 1, "Command timed out"
    except FileNotFoundError:
        return 1, "docker command not found"
    except Exception as exc:
        return 1, str(exc)


def docker_ps() -> list[Container]:
    """Return a list of containers from ``docker compose ps``."""
    rc, out = _run([
        "docker", "ps", "-a",
        "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.State}}",
    ])
    containers: list[Container] = []
    if rc != 0 or not out:
        return containers
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        containers.append(Container(
            name=parts[0],
            image=parts[1],
            status=parts[2],
            ports=parts[3],
            state=parts[4],
        ))
    return containers


def docker_logs(container: str, n: int = 200) -> str:
    """Return last *n* log lines from a container."""
    rc, out = _run(["docker", "logs", "--tail", str(n), container])
    return out


def docker_exec(container: str, cmd: str) -> tuple[int, str]:
    """Execute *cmd* inside *container* via ``docker exec``."""
    import shlex

    parts = shlex.split(cmd)
    rc, out = _run(["docker", "exec", "-i", container] + parts)
    return rc, out


def docker_inspect(container: str) -> str:
    """Return JSON inspect output for a container."""
    rc, out = _run(["docker", "inspect", container])
    return out


def docker_restart(container: str) -> tuple[int, str]:
    """Restart a container via ``docker restart``."""
    return _run(["docker", "restart", container])


def docker_recreate(container: str, compose_file: Optional[str] = None) -> tuple[int, str]:
    """Stop + recreate a container via ``docker compose up -d --force-recreate``.

    ``docker compose`` expects a *service* name, not a container name.
    The service name is resolved from the ``com.docker.compose.service`` label
    set by Docker Compose when the container was created.
    """
    # Resolve compose service name from the container label.
    rc_inspect, service_label = _run([
        "docker", "inspect", container,
        "--format", "{{index .Config.Labels \"com.docker.compose.service\"}}",
    ])
    service = service_label.strip() if rc_inspect == 0 and service_label.strip() else container

    args = ["docker", "compose"]
    if compose_file:
        args += ["-f", compose_file]
    args += ["up", "-d", "--force-recreate", service]
    # Run from the project root so docker compose picks up the default compose file
    cwd = str(Path(__file__).resolve().parents[2])  # repo root
    return _run(args, cwd=cwd)


def docker_redis_cli(args: str, password: str, db: int = 0) -> tuple[int, str]:
    """Execute a redis-cli command inside the gsage-redis container.

    Args:
        args: space-separated redis-cli arguments, e.g. "INFO server" or "DBSIZE"
        password: Redis requirepass value
        db: Redis DB number (default 0)
    """
    import shlex  # noqa: PLC0415

    parts = shlex.split(args)
    cmd = ["docker", "exec", "gsage-redis", "redis-cli", "-a", password, "-n", str(db)] + parts
    return _run(cmd)
