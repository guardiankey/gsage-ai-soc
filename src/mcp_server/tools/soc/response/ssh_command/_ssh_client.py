"""gSage AI — SSH client helper for the ssh_command tools.

Wraps asyncssh to provide a single-shot command execution function.
Supports both password and private-key authentication.
No interactive sessions, no PTY allocation.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Maximum time to wait for the TCP connection to be established (seconds).
_DEFAULT_CONNECT_TIMEOUT = 10
# Maximum time to wait for the remote command to complete (seconds).
_DEFAULT_COMMAND_TIMEOUT = 30
# Bytes to read at most from each of stdout / stderr.
_DEFAULT_MAX_OUTPUT_BYTES = 65_536


@dataclass
class SSHCommandResult:
    """Result of a single remote command execution."""

    stdout: str
    stderr: str
    exit_code: int
    truncated: bool  # True if output was cut at max_output_bytes


async def run_ssh_command(
    *,
    hostname: str,
    connect_hostname: Optional[str] = None,
    port: int = 22,
    username: str,
    auth_method: str,  # "password" | "key"
    password: Optional[str] = None,
    private_key_path: Optional[str] = None,
    private_key_b64: Optional[str] = None,
    private_key_passphrase: Optional[str] = None,
    command: str,
    connect_timeout: int = _DEFAULT_CONNECT_TIMEOUT,
    command_timeout: int = _DEFAULT_COMMAND_TIMEOUT,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> SSHCommandResult:
    """Open an SSH connection, run *command*, close, and return the result.

    Args:
        hostname: Remote host label (IP or FQDN) used in logs and error messages.
        connect_hostname: IP address or host to use for the actual TCP connection.
            When provided (e.g. pre-resolved IP from CIDR validation), this overrides
            ``hostname`` for the connection only, preventing DNS rebinding. When omitted,
            ``hostname`` is used for both connection and logging.
        port: SSH port (default 22).
        username: Remote user.
        auth_method: ``"password"`` or ``"key"``.
        password: Password if ``auth_method == "password"``.
        private_key_path: Filesystem path to a PEM/OpenSSH private key if
            ``auth_method == "key"``.
        private_key_b64: Base64-encoded PEM/OpenSSH private key content (alternative
            to ``private_key_path`` when filesystem access is not available).
        private_key_passphrase: Optional passphrase for the private key.
        command: Shell command to execute remotely.
        connect_timeout: Seconds to wait for TCP + SSH handshake.
        command_timeout: Seconds to wait for the command to finish.
        max_output_bytes: Maximum bytes kept from stdout and stderr combined.

    Returns:
        :class:`SSHCommandResult` with stdout, stderr, exit_code, truncated flag.

    Raises:
        ImportError: If ``asyncssh`` is not installed.
        asyncssh.DisconnectError, asyncssh.PermissionDenied, etc. — caller
            should catch ``Exception`` and wrap in ToolResult.failure.
    """
    try:
        import asyncssh  # noqa: PLC0415 — optional dependency
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "asyncssh is required for ssh_command tools. "
            "Add 'asyncssh' to requirements.txt and rebuild the container."
        ) from exc

    import asyncio  # noqa: PLC0415

    # ── Build connection kwargs ─────────────────────────────────────────────
    connect_kwargs: dict = {
        "host": connect_hostname or hostname,  # Use pre-resolved IP when available
        "port": port,
        "username": username,
        "known_hosts": None,  # Disable host-key checking (keys managed by operator)
        "connect_timeout": connect_timeout,
    }

    if auth_method == "password":
        if not password:
            raise ValueError("auth_method='password' but no password provided")
        connect_kwargs["password"] = password
        connect_kwargs["preferred_auth"] = "password"
    elif auth_method == "key":
        if private_key_path:
            key = asyncssh.read_private_key(private_key_path, passphrase=private_key_passphrase)
        elif private_key_b64:
            try:
                key_bytes = base64.b64decode(private_key_b64)
            except Exception as exc:
                raise ValueError(f"private_key_b64 is not valid base64: {exc}") from exc
            key = asyncssh.import_private_key(key_bytes, passphrase=private_key_passphrase)
        else:
            raise ValueError(
                "auth_method='key' requires either 'private_key_path' or 'private_key_b64'."
            )
        connect_kwargs["client_keys"] = [key]
        connect_kwargs["preferred_auth"] = "publickey"
    else:
        raise ValueError(f"Unsupported auth_method: {auth_method!r}. Use 'password' or 'key'.")

    # ── Execute ─────────────────────────────────────────────────────────────
    async with asyncssh.connect(**connect_kwargs) as conn:
        try:
            result = await asyncio.wait_for(
                conn.run(command, check=False, request_pty=False),
                timeout=command_timeout,
            )
        except asyncio.TimeoutError:
            log.warning("ssh_command: command timed out after %ss on %s", command_timeout, hostname)
            raise TimeoutError(
                f"Command timed out after {command_timeout}s on {hostname}"
            )

    # ── Collect & truncate output ────────────────────────────────────────────
    # asyncssh returns str in text mode (default encoding).  We treat
    # max_output_bytes as a character limit (close enough for ASCII/UTF-8 logs).
    out_str: str = result.stdout or ""  # type: ignore[assignment]
    err_str: str = result.stderr or ""  # type: ignore[assignment]
    truncated = False

    # Apply per-stream truncation (max_output_bytes split 50/50 between out/err)
    half = max_output_bytes // 2
    if len(out_str) > half:
        out_str = out_str[:half]
        truncated = True
    if len(err_str) > half:
        err_str = err_str[:half]
        truncated = True

    return SSHCommandResult(
        stdout=out_str,
        stderr=err_str,
        exit_code=result.exit_status if result.exit_status is not None else -1,
        truncated=truncated,
    )
