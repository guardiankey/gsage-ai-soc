"""gSage AI — SSH Preset Command tool (no approval required).

Executes pre-configured command templates on remote hosts via SSH.
Commands must be defined in the tool configuration and cannot be modified
by the LLM — only argument values (with strict sanitization) are dynamic.

Requires permission: ``ssh:execute``
Does NOT require human approval.
"""

from __future__ import annotations

import time
import logging
from typing import ClassVar, Optional

from src.mcp_server.tools.soc.response.ssh_command._base import SSHCommandBase
from src.mcp_server.tools.soc.response.ssh_command._sanitizer import (
    DEFAULT_ARG_SANITIZE_PATTERN,
    interpolate_preset,
    sanitize_argument,
)
from src.shared.security.context import AgentContext
from src.mcp_server.tools.base import ToolResult

log = logging.getLogger(__name__)


class SSHCommandPresetTool(SSHCommandBase):
    """Execute a pre-configured SSH command preset on a remote host.

    All executable commands are defined by the operator in the tool
    configuration.  The LLM selects a ``host_key`` and ``command_key`` and
    may supply ``arguments`` whose values are strictly sanitized before
    being interpolated into the command template.

    This tool does **not** require human-in-the-loop approval.  For arbitrary
    command execution with approval, use the ``ssh_command`` tool instead.
    """

    name: ClassVar[str] = "ssh_command_preset"
    summary: ClassVar[str] = "Execute a pre-configured SSH command preset on a remote host (no approval required)"
    category: ClassVar[str] = "network"
    requires_approval: ClassVar[bool] = False

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["host_key", "command_key"],
        "properties": {
            "host_key": {
                "type": "string",
                "description": (
                    "Key of the SSH host to connect to, as configured in this tool's settings. "
                    "Available host keys are listed in the tool configuration."
                ),
            },
            "command_key": {
                "type": "string",
                "description": (
                    "Key of the preset command to execute, as defined in this tool's settings. "
                    "Available command keys and their descriptions are listed in the tool "
                    "configuration. Example: 'list_processes', 'disk_usage', 'check_service'."
                ),
            },
            "arguments": {
                "type": "object",
                "description": (
                    "Optional key-value pairs to fill placeholders in the command template. "
                    "Each value is sanitized against the preset's argument_sanitize_pattern "
                    "before being interpolated. "
                    "Example: {\"filter\": \"nginx\"} for template 'ps auxw | grep {filter}'."
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
        command_key: str = params.get("command_key", "").strip()
        arguments: dict = params.get("arguments") or {}

        if not command_key:
            return self._failure("MISSING_PARAM", "'command_key' is required.")

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

        host_label = host_key or hostname

        # ── Resolve target host ───────────────────────────────────────────
        host, connect_hostname, err = await self._resolve_target(
            config, host_key, hostname, credential_key
        )
        if err:
            error_code = "HOST_NOT_FOUND" if host_key else "HOST_NOT_ALLOWED"
            return self._failure(error_code, err)
        assert host is not None

        # ── Resolve preset ───────────────────────────────────────────────
        preset, err = self._resolve_preset(config, command_key)
        if err:
            return self._failure("PRESET_NOT_FOUND", err)
        assert preset is not None

        # ── Check preset is allowed on this host (fixed host_key only) ────────────────
        allowed_hosts: list[str] | None = preset.get("allowed_hosts")
        if allowed_hosts and host_key and host_key not in allowed_hosts:
            return self._failure(
                "HOST_NOT_ALLOWED",
                f"Preset {command_key!r} is not allowed on host {host_key!r}. "
                f"Allowed hosts for this preset: {', '.join(allowed_hosts)}",
            )

        # ── Sanitize arguments ──────────────────────────────────────────────
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

        # ── Interpolate command template ────────────────────────────────────
        template: str = preset.get("command_template", "")
        command = interpolate_preset(template, sanitized_args)

        # ── Security checks ─────────────────────────────────────────────────
        err = self._security_check(command, config)
        if err:
            log.warning(
                "ssh_command_preset: blocked command on host=%r preset=%r: %s",
                host_label, command_key, err,
            )
            return self._failure("COMMAND_BLOCKED", err)

        # ── Execute via SSH ─────────────────────────────────────────────────
        log.info(
            "ssh_command_preset: org=%s user=%s host=%r preset=%r",
            agent_context.org_id,
            agent_context.user_id,
            host_label,
            command_key,
        )
        result, err = await self._run_ssh(host, command, config, connect_hostname)
        if err:
            return self._failure("SSH_ERROR", err, retryable=False)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        data = self._build_output(host_label, command, result, elapsed_ms)  # type: ignore[arg-type]
        data["command_key"] = command_key
        data["preset_description"] = preset.get("description", "")

        if result.exit_code != 0:  # type: ignore[union-attr]
            return self._partial(
                data,
                "NONZERO_EXIT",
                f"Command exited with code {result.exit_code}.",  # type: ignore[union-attr]
                retryable=False,
            )

        return self._success(data, elapsed_ms)
