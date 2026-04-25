"""gSage AI — SSH Command tool (human approval required).

Executes arbitrary shell commands on remote hosts via SSH.
Supports both free-form commands (subject to allow/deny regex lists) and
preset command keys as a convenience alias.

Requires permission: ``ssh:execute``
Requires human-in-the-loop approval before execution.
"""

from __future__ import annotations

import time
import logging
from typing import ClassVar, Optional

from src.mcp_server.tools.soc.response.ssh_command._base import SSHCommandBase
from src.mcp_server.tools.soc.response.ssh_command._sanitizer import (
    DEFAULT_ARG_SANITIZE_PATTERN,
    check_config_allow,
    interpolate_preset,
    sanitize_argument,
)
from src.shared.security.context import AgentContext
from src.mcp_server.tools.base import ToolResult

log = logging.getLogger(__name__)


class SSHCommandTool(SSHCommandBase):
    """Execute an arbitrary shell command on a remote host via SSH.

    The command is checked against the operator-configured
    ``allowed_command_patterns`` (at least one must match) and
    ``denied_command_patterns`` (none may match) before being sent to the
    remote host.  The hardcoded security deny list is also always enforced.

    This tool **requires human-in-the-loop approval** before execution.
    For non-interactive preset commands without approval, use
    ``ssh_command_preset`` instead.

    Alternatively, supply ``command_key`` instead of ``command`` to run a
    configured preset through this approval-gated tool.
    """

    name: ClassVar[str] = "ssh_command"
    summary: ClassVar[str] = "Execute an arbitrary shell command on a remote host via SSH (requires config and human approval)"
    category: ClassVar[str] = "network"
    requires_approval: ClassVar[bool] = True

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": [],
        "properties": {
            "host_key": {
                "type": "string",
                "description": (
                    "Key of a configured SSH host to connect to. "
                    "Use this OR (hostname + credential_key), not both. "
                    "Available host keys are listed in the tool configuration."
                ),
            },
            "hostname": {
                "type": "string",
                "description": (
                    "Hostname or IP of the target host (dynamic access). "
                    "Must match an allowed_host_patterns regex or resolve to an IP "
                    "within allowed_cidrs. Use this OR host_key, not both. "
                    "Requires credential_key."
                ),
            },
            "credential_key": {
                "type": "string",
                "description": (
                    "Key of the credential profile to use for authentication. "
                    "Required when hostname is provided. "
                    "Available credential keys are listed in the tool configuration."
                ),
            },
            "command": {
                "type": "string",
                "description": (
                    "Shell command to execute on the remote host. "
                    "Must satisfy the operator-configured allowed_command_patterns "
                    "and must NOT match any denied_command_patterns. "
                    "Example: 'tail -100 /var/log/syslog | grep ERROR'"
                ),
                "minLength": 1,
                "maxLength": 4096,
            },
            "command_key": {
                "type": "string",
                "description": (
                    "Alternatively, specify a preset command key to run a pre-configured "
                    "command template through this approval-gated tool. "
                    "Mutually exclusive with 'command'."
                ),
            },
            "arguments": {
                "type": "object",
                "description": (
                    "Argument values for preset command template placeholders "
                    "(only used when command_key is provided). "
                    "Each value is sanitized before interpolation."
                ),
                "additionalProperties": {"type": "string"},
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

        host_key: str = params.get("host_key", "").strip()
        hostname: str = params.get("hostname", "").strip()
        credential_key: str = params.get("credential_key", "").strip()
        command_raw: Optional[str] = params.get("command")
        command_key: Optional[str] = params.get("command_key")
        arguments: dict = params.get("arguments") or {}

        # ── Validate host selection ──────────────────────────────────────────
        if host_key and (hostname or credential_key):
            return self._failure(
                "INVALID_PARAM",
                "'host_key' and 'hostname'/'credential_key' are mutually exclusive. "
                "Provide either host_key OR (hostname + credential_key).",
            )
        if not host_key and not hostname:
            return self._failure(
                "MISSING_PARAM",
                "Either 'host_key' or 'hostname' + 'credential_key' must be provided.",
            )
        if hostname and not credential_key:
            return self._failure(
                "MISSING_PARAM",
                "'credential_key' is required when using dynamic 'hostname'.",
            )

        if not command_raw and not command_key:
            return self._failure(
                "MISSING_PARAM",
                "Either 'command' or 'command_key' must be provided.",
            )
        if command_raw and command_key:
            return self._failure(
                "INVALID_PARAM",
                "'command' and 'command_key' are mutually exclusive. Provide only one.",
            )

        host_label = host_key or hostname

        # ── Resolve target host ──────────────────────────────────────────────
        host, connect_hostname, err = await self._resolve_target(
            config, host_key, hostname, credential_key
        )
        if err:
            error_code = "HOST_NOT_FOUND" if host_key else "HOST_NOT_ALLOWED"
            return self._failure(error_code, err)
        assert host is not None

        # ── Build final command ─────────────────────────────────────────────
        preset_description = ""

        if command_key:
            # Preset path — resolve, sanitize args, interpolate
            preset, err = self._resolve_preset(config, command_key)
            if err:
                return self._failure("PRESET_NOT_FOUND", err)
            assert preset is not None

            # Check preset is allowed on this host (fixed host_key only)
            allowed_hosts: list[str] | None = preset.get("allowed_hosts")
            if allowed_hosts and host_key and host_key not in allowed_hosts:
                return self._failure(
                    "HOST_NOT_ALLOWED",
                    f"Preset {command_key!r} is not allowed on host {host_key!r}. "
                    f"Allowed hosts for this preset: {', '.join(allowed_hosts)}",
                )

            sanitize_pattern: str = preset.get(
                "argument_sanitize_pattern", DEFAULT_ARG_SANITIZE_PATTERN
            )
            sanitized_args: dict[str, str] = {}
            for arg_name, arg_value in arguments.items():
                clean, err = sanitize_argument(str(arg_value), sanitize_pattern)
                if err:
                    return self._failure(
                        "INVALID_ARGUMENT",
                        f"Argument {arg_name!r}: {err}",
                    )
                sanitized_args[arg_name] = clean

            command = interpolate_preset(preset.get("command_template", ""), sanitized_args)
            preset_description = preset.get("description", "")

        else:
            # Free-form command path
            command = command_raw.strip()  # type: ignore[union-attr]
            if not command:
                return self._failure("MISSING_PARAM", "'command' must not be empty.")

            # Check allow list (only for free-form commands, not presets)
            allowed_patterns: list[str] = config.get("allowed_command_patterns", [])
            err = check_config_allow(command, allowed_patterns)
            if err:
                return self._failure("COMMAND_NOT_ALLOWED", err)

        # ── Security checks (hardcoded + config deny — applies to both paths) ─
        err = self._security_check(command, config)
        if err:
            log.warning(
                "ssh_command: blocked command on host=%r: %s",
                host_label,
                err,
            )
            return self._failure("COMMAND_BLOCKED", err)

        # ── Execute via SSH ─────────────────────────────────────────────────
        log.info(
            "ssh_command: org=%s user=%s host=%r command=%r",
            agent_context.org_id,
            agent_context.user_id,
            host_label,
            command[:120],
        )
        result, err = await self._run_ssh(host, command, config, connect_hostname)
        if err:
            return self._failure("SSH_ERROR", err, retryable=False)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        data = self._build_output(host_label, command, result, elapsed_ms)  # type: ignore[arg-type]
        if command_key:
            data["command_key"] = command_key
            data["preset_description"] = preset_description

        if result.exit_code != 0:  # type: ignore[union-attr]
            return self._partial(
                data,
                "NONZERO_EXIT",
                f"Command exited with code {result.exit_code}.",  # type: ignore[union-attr]
                retryable=False,
            )

        return self._success(data, elapsed_ms)
