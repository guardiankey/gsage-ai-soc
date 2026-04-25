"""gSage AI — PowerShell-over-SSH runner for ad_write.

Connects to a Windows jump host that has OpenSSH Server + PowerShell 7 +
the RSAT ActiveDirectory module installed.  Submits a pwsh script,
captures its JSON output (via ``ConvertTo-Json``) and returns the parsed
result.

Why SSH + pwsh instead of LDAP writes?
--------------------------------------
* ``Set-ADUser``, ``Unlock-ADAccount`` and ``Disable-ADAccount`` handle
  userAccountControl bits, lockout state, pwdLastSet, and moving users
  between OUs in ways that map 1:1 to AD intent.
* Uniform error handling via non-zero exit codes + structured JSON on
  stderr / stdout.
* The project already ships asyncssh.  No local pwsh in the mcp_server
  container — the script runs entirely on the Windows jump host.

Security
--------
* Public-key authentication only (no passwords).
* Optional ``known_hosts`` pinning.
* Scripts are encoded as UTF-16LE base64 and passed via ``-EncodedCommand``
  to avoid quoting issues across the SSH transport.
* Script output is parsed from the last JSON object emitted, so
  ``Write-Warning``/``Write-Verbose`` noise doesn't break the contract.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------

@dataclass
class PwshConnectionConfig:
    """Decoded subset of the tool config used for SSH/pwsh execution."""

    host: str
    port: int
    user: str
    private_key: str
    known_hosts: str = ""
    command_timeout: int = 60

    @classmethod
    def from_tool_config(cls, config: dict) -> "PwshConnectionConfig":
        missing = [
            k for k in ("ssh_host", "ssh_user", "ssh_private_key")
            if not config.get(k)
        ]
        if missing:
            raise PwshError(
                "CONFIG_INCOMPLETE",
                f"SSH/pwsh config incomplete — missing: {', '.join(missing)}",
            )
        return cls(
            host=config["ssh_host"],
            port=int(config.get("ssh_port", 22) or 22),
            user=config["ssh_user"],
            private_key=config["ssh_private_key"],
            known_hosts=config.get("ssh_known_hosts") or "",
            command_timeout=int(config.get("ssh_command_timeout_seconds", 60) or 60),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PwshError(Exception):
    """Raised when the pwsh script execution fails in a structured way."""

    def __init__(self, code: str, message: str, *, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PwshResult:
    """Structured outcome of a pwsh-over-SSH execution."""

    exit_code: int
    stdout: str
    stderr: str
    data: dict  # Parsed from the last JSON object in stdout


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _encode_pwsh_script(script: str) -> str:
    """Encode a pwsh script for ``-EncodedCommand`` (UTF-16LE + base64)."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _extract_json_object(text: str) -> dict:
    """Pull the last top-level JSON object / array from *text*.

    We can't rely on the full stdout being pure JSON — pwsh cmdlets occasionally
    emit progress records or warnings.  Our script contract is:

    * The last non-empty line of stdout is a JSON document (object or array).
    * Everything earlier is diagnostic.

    If the tail isn't JSON, try to parse the whole stream.
    """
    stripped = text.strip()
    if not stripped:
        raise PwshError("INVALID_OUTPUT", "pwsh script produced no output.")

    # Fast path: whole output is a JSON document.
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None

    if parsed is None:
        # Last-line fallback.
        last_line = stripped.splitlines()[-1].strip()
        try:
            parsed = json.loads(last_line)
        except json.JSONDecodeError as exc:
            raise PwshError(
                "INVALID_OUTPUT",
                f"pwsh output is not JSON: {exc}",
                details={"stdout_tail": stripped[-2000:]},
            ) from exc

    # Always return a dict — wrap arrays.
    if isinstance(parsed, list):
        return {"items": parsed}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


async def run_pwsh_script(
    *,
    config: PwshConnectionConfig,
    script: str,
) -> PwshResult:
    """Execute *script* on the Windows jump host via SSH + pwsh.

    Args:
        config: Decoded SSH connection config (see
            :meth:`PwshConnectionConfig.from_tool_config`).
        script: The pwsh script body.  MUST end with a single
            ``ConvertTo-Json -Depth N`` call so the runner can parse
            the result.

    Returns:
        :class:`PwshResult` with the parsed JSON payload in ``data``.

    Raises:
        :class:`PwshError`: on connection, auth, timeout, non-zero exit,
            or JSON-parse failure.
    """
    try:
        import asyncssh  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise PwshError(
            "DEPENDENCY_MISSING",
            "asyncssh is required for ad_write. Add 'asyncssh' to requirements.txt.",
        ) from exc

    # Import private key from the in-memory PEM (no filesystem writes).
    try:
        client_key = asyncssh.import_private_key(config.private_key)
    except Exception as exc:
        raise PwshError(
            "AUTH_KEY_INVALID",
            f"Failed to parse ssh_private_key: {exc}",
        ) from exc

    # Optional known_hosts pinning — write to a tmp file only for the
    # duration of this call.  asyncssh accepts a path, not raw bytes.
    known_hosts_path: Optional[str] = None
    tmp_file: Optional[Any] = None
    if config.known_hosts.strip():
        try:
            tmp_file = tempfile.NamedTemporaryFile(
                "w", suffix="_known_hosts", delete=False, encoding="utf-8"
            )
            tmp_file.write(config.known_hosts)
            tmp_file.flush()
            tmp_file.close()
            known_hosts_path = tmp_file.name
        except Exception as exc:
            raise PwshError(
                "CONFIG_INVALID",
                f"Failed to materialize known_hosts: {exc}",
            ) from exc

    # Strict host-key checking when known_hosts is provided, else None
    # (disabled — operator is responsible for the network path).
    kh_arg: Any = known_hosts_path if known_hosts_path else None

    encoded = _encode_pwsh_script(script)
    remote_cmd = f"pwsh -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"

    try:
        log.info(
            "ad_write.pwsh: connecting host=%s:%d user=%s",
            config.host, config.port, config.user,
        )
        async with asyncssh.connect(
            host=config.host,
            port=config.port,
            username=config.user,
            client_keys=[client_key],
            known_hosts=kh_arg,
            connect_timeout=10,
            preferred_auth="publickey",
        ) as conn:
            try:
                result = await asyncio.wait_for(
                    conn.run(remote_cmd, check=False, request_pty=False),
                    timeout=config.command_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise PwshError(
                    "PWSH_TIMEOUT",
                    f"pwsh script timed out after {config.command_timeout}s.",
                ) from exc
    except PwshError:
        raise
    except Exception as exc:  # asyncssh exceptions land here
        raise PwshError("SSH_FAILED", f"SSH execution failed: {exc}") from exc
    finally:
        if known_hosts_path:
            try:
                Path(known_hosts_path).unlink(missing_ok=True)
            except Exception:  # pragma: no cover
                log.debug("ad_write.pwsh: tmp known_hosts cleanup failed", exc_info=True)

    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)

    stdout = _as_text(result.stdout)
    stderr = _as_text(result.stderr)
    exit_code = result.exit_status if result.exit_status is not None else -1

    if exit_code != 0:
        raise PwshError(
            "PWSH_NONZERO_EXIT",
            f"pwsh script exited with code {exit_code}.",
            details={
                "exit_code": exit_code,
                "stderr_tail": stderr[-2000:],
                "stdout_tail": stdout[-2000:],
            },
        )

    try:
        data = _extract_json_object(stdout)
    except PwshError as exc:
        exc.details.setdefault("stderr_tail", stderr[-2000:])
        raise

    return PwshResult(exit_code=exit_code, stdout=stdout, stderr=stderr, data=data)
