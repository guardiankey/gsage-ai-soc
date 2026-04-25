"""gSage AI — SSHCommandBase abstract tool.

Shared config schema, host resolution, security checks, and SSH execution
logic used by both SSHCommandPresetTool and SSHCommandTool.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
import uuid
from abc import abstractmethod
from typing import ClassVar, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.response.ssh_command._sanitizer import (
    check_config_deny,
    check_hardcoded_deny,
)
from src.mcp_server.tools.soc.response.ssh_command._ssh_client import (
    SSHCommandResult,
    run_ssh_command,
)
from src.shared.models import GSageToolConfig
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class SSHCommandBase(BaseTool):
    """Abstract base for SSH remote command tools.

    Provides the shared config schema, config defaults, host resolution,
    security validation, and SSH execution wrapper.

    Concrete subclasses must declare ``name``, ``requires_approval``,
    ``params_schema``, and ``execute()``.
    """

    # ── Tool metadata ────────────────────────────────────────────────────────
    version: ClassVar[str] = "1.0.0"
    permissions: ClassVar[list[str]] = ["ssh:execute"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "host_key"}

    # ── Config schema ────────────────────────────────────────────────────────
    config_schema: ClassVar[Optional[dict]] = {
        "hosts": {
            "type": "array",
            "description": "List of SSH hosts available for this tool.",
            "items": {
                "type": "object",
                "required": ["key", "hostname", "username", "auth_method"],
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Unique identifier for this host (e.g. 'web-prod-1').",
                    },
                    "hostname": {
                        "type": "string",
                        "description": "IP address or FQDN of the remote host.",
                    },
                    "port": {
                        "type": "integer",
                        "description": "SSH port. Default: 22.",
                        "minimum": 1,
                        "maximum": 65535,
                    },
                    "username": {
                        "type": "string",
                        "description": "Remote username to authenticate as.",
                    },
                    "auth_method": {
                        "type": "string",
                        "description": "'password' or 'key'.",
                    },
                    "password": {
                        "type": "string",
                        "description": "Password (only when auth_method='password').",
                    },
                    "private_key_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the private key file on the container "
                            "filesystem (only when auth_method='key'). "
                            "Mount the key as a Docker secret or volume."
                        ),
                    },
                    "private_key_b64": {
                        "type": "string",
                        "description": (
                            "Base64-encoded PEM/OpenSSH private key content "
                            "(alternative to private_key_path when filesystem access "
                            "is not available). Encode with: "
                            "base64 -w0 ~/.ssh/id_ed25519"
                        ),
                    },
                    "private_key_passphrase": {
                        "type": "string",
                        "description": "Passphrase for the private key, if encrypted.",
                    },
                },
            },
        },
        "preset_commands": {
            "type": "array",
            "description": "Pre-defined commands that can be run without approval.",
            "items": {
                "type": "object",
                "required": ["key", "description", "command_template"],
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Unique key identifying this preset (e.g. 'list_processes').",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description shown to the LLM.",
                    },
                    "command_template": {
                        "type": "string",
                        "description": (
                            "Shell command template. Use {argument_name} for dynamic parts. "
                            "Example: 'ps auxw | grep {filter}'"
                        ),
                    },
                    "argument_sanitize_pattern": {
                        "type": "string",
                        "description": (
                            "Regex pattern that each argument value must fully match. "
                            "Default: ^[a-zA-Z0-9._\\-/: ]+$"
                        ),
                    },
                    "allowed_hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of host keys where this preset is allowed. "
                            "When omitted, the preset is allowed on all configured hosts."
                        ),
                    },
                },
            },
        },
        "allowed_command_patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Regex allow list for arbitrary commands (used by ssh_command tool only). "
                "At least one pattern must match the command for it to be executed. "
                "When empty, all commands are allowed (subject to the deny list). "
                "Example: ['^ps\\\\b', '^tail\\\\b', '^cat\\\\s+/var/log/']"
            ),
        },
        "denied_command_patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Regex deny list applied to ALL commands on both tools. "
                "Commands matching any pattern are blocked. "
                "Example: ['^sudo\\\\b', 'wget\\\\s+.*\\\\|\\\\s*(ba)?sh']"
            ),
        },
        "connection_timeout_seconds": {
            "type": "integer",
            "description": "Seconds to wait for SSH TCP connection. Default: 10.",
            "minimum": 1,
            "maximum": 60,
        },
        "command_timeout_seconds": {
            "type": "integer",
            "description": "Seconds to wait for the remote command to complete. Default: 30.",
            "minimum": 1,
            "maximum": 300,
        },
        "max_output_bytes": {
            "type": "integer",
            "description": "Maximum bytes to capture from stdout + stderr combined. Default: 65536.",
            "minimum": 1024,
            "maximum": 1048576,
        },
        "credential_profiles": {
            "type": "array",
            "description": (
                "Named credential profiles for dynamic host access. "
                "The LLM selects a profile by key when using the 'hostname' parameter."
            ),
            "items": {
                "type": "object",
                "required": ["key", "username", "auth_method"],
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Unique identifier for this credential profile (e.g. 'prod', 'dev').",
                    },
                    "username": {
                        "type": "string",
                        "description": "Remote username to authenticate as.",
                    },
                    "auth_method": {
                        "type": "string",
                        "description": "'password' or 'key'.",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Default SSH port for hosts using this profile. Default: 22.",
                        "minimum": 1,
                        "maximum": 65535,
                    },
                    "password": {
                        "type": "string",
                        "description": "Password (only when auth_method='password').",
                    },
                    "private_key_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the private key file on the container filesystem "
                            "(only when auth_method='key'). Mount via Docker secret or volume."
                        ),
                    },
                    "private_key_b64": {
                        "type": "string",
                        "description": (
                            "Base64-encoded PEM/OpenSSH private key content "
                            "(alternative to private_key_path). "
                            "Encode with: base64 -w0 ~/.ssh/id_ed25519"
                        ),
                    },
                    "private_key_passphrase": {
                        "type": "string",
                        "description": "Passphrase for encrypted private keys.",
                    },
                },
            },
        },
        "allowed_host_patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Regex patterns tested with re.fullmatch (case-insensitive) against the "
                "hostname supplied by the LLM for dynamic host access. "
                "When a pattern matches, the hostname is allowed without DNS resolution. "
                "Use anchored patterns to prevent substring bypass. "
                "Example: ['^192\\\\.168\\\\.10\\\\.\\\\d+$', '^[a-z0-9-]+\\\\.internal\\\\.example\\\\.com$']"
            ),
        },
        "allowed_cidrs": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "IPv4/IPv6 CIDR blocks that a dynamic hostname must resolve into. "
                "When the hostname does not match any allowed_host_patterns, it is resolved "
                "via DNS and the resulting IP is checked against these CIDRs. "
                "The resolved IP is used for the SSH connection to prevent DNS rebinding. "
                "Example: ['192.168.10.0/24', '10.0.0.0/8']"
            ),
        },
    }

    config_defaults: ClassVar[dict] = {
        "hosts": [],
        "preset_commands": [],
        "credential_profiles": [],
        "allowed_host_patterns": [],
        "allowed_cidrs": [],
        "allowed_command_patterns": [],
        "denied_command_patterns": [],
        "connection_timeout_seconds": 10,
        "command_timeout_seconds": 30,
        "max_output_bytes": 65_536,
    }

    # ── Abstract interface ───────────────────────────────────────────────────

    async def enrich_for_listing(
        self,
        org_id: uuid.UUID,
        session: AsyncSession,
    ) -> Optional[str]:
        """Inject available hosts, presets and credential profiles into the tool
        description at ``list_tools`` time so the LLM knows what is available
        without needing to probe the tool with invalid parameters."""
        stmt = select(GSageToolConfig).where(
            GSageToolConfig.org_id == org_id,
            GSageToolConfig.tool_name == self.name,
            GSageToolConfig.profile_id == "default",
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None

        config: dict = row.config  # property decrypts on access
        lines: list[str] = []

        hosts = config.get("hosts", [])
        if hosts:
            host_keys = ", ".join(h["key"] for h in hosts if h.get("key"))
            if host_keys:
                lines.append(f"Available hosts (host_key): {host_keys}")

        presets = config.get("preset_commands", [])
        if presets:
            lines.append("Available presets (command_key):")
            for p in presets:
                desc = p.get("description", "")
                lines.append(f"  - {p['key']}: {desc}" if desc else f"  - {p['key']}")

        creds = config.get("credential_profiles", [])
        if creds:
            cred_keys = ", ".join(c["key"] for c in creds if c.get("key"))
            if cred_keys:
                lines.append(f"Credential profiles (credential_key): {cred_keys}")

        has_patterns = bool(
            config.get("allowed_host_patterns") or config.get("allowed_cidrs")
        )
        if has_patterns:
            lines.append(
                "Dynamic host access is enabled: pass hostname + credential_key "
                "instead of host_key to connect to a dynamically specified host."
            )

        if not lines:
            return None

        return "\n".join(lines)

    @abstractmethod
    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """Subclasses implement the tool-specific logic here."""

    # ── Shared helpers ───────────────────────────────────────────────────────

    def _resolve_host(self, config: dict, host_key: str) -> tuple[Optional[dict], Optional[str]]:
        """Find the host config entry by key.

        Returns:
            ``(host_dict, None)`` if found, ``(None, error_message)`` if not.
        """
        hosts: list[dict] = config.get("hosts", [])
        if not hosts:
            return None, (
                "No hosts are configured for this tool. "
                "Add at least one host entry via the tool configuration in the admin console."
            )
        for host in hosts:
            if host.get("key") == host_key:
                return host, None
        available = ", ".join(h.get("key", "?") for h in hosts)
        return None, (
            f"Host key {host_key!r} not found in configuration. "
            f"Available hosts: {available}"
        )

    def _resolve_preset(
        self, config: dict, command_key: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Find the preset command config entry by key.

        Returns:
            ``(preset_dict, None)`` if found, ``(None, error_message)`` if not.
        """
        presets: list[dict] = config.get("preset_commands", [])
        if not presets:
            return None, (
                "No preset commands are configured for this tool. "
                "Add preset_commands entries via the admin console tool configuration."
            )
        for preset in presets:
            if preset.get("key") == command_key:
                return preset, None
        available = ", ".join(p.get("key", "?") for p in presets)
        return None, (
            f"Command key {command_key!r} not found in configuration. "
            f"Available commands: {available}"
        )

    def _resolve_credential(
        self, config: dict, credential_key: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Find a credential profile by key.

        Returns:
            ``(profile_dict, None)`` if found, ``(None, error_message)`` if not.
        """
        profiles: list[dict] = config.get("credential_profiles", [])
        if not profiles:
            return None, (
                "No credential_profiles are configured. "
                "Add entries under credential_profiles in the tool configuration "
                "to enable dynamic host access."
            )
        for profile in profiles:
            if profile.get("key") == credential_key:
                return profile, None
        available = ", ".join(p.get("key", "?") for p in profiles)
        return None, (
            f"Credential key {credential_key!r} not found in configuration. "
            f"Available credential profiles: {available}"
        )

    async def _validate_dynamic_host(
        self, config: dict, hostname: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Validate a dynamic hostname against operator-configured allow rules.

        Validation order:
        1. ``allowed_host_patterns`` — regex fullmatch (case-insensitive, no DNS needed).
           Returns the original hostname as connect target.
        2. ``allowed_cidrs`` — DNS resolution then IP-in-CIDR check.
           Returns the **resolved IP** to prevent DNS rebinding.

        Returns:
            ``(connect_hostname, None)`` on success.
            ``(None, error_message)`` if the host is not allowed or DNS fails.
        """
        # 1. Regex patterns — no DNS resolution needed
        patterns: list[str] = config.get("allowed_host_patterns", [])
        for raw_pattern in patterns:
            try:
                if re.fullmatch(raw_pattern, hostname, re.IGNORECASE):
                    return hostname, None
            except re.error:
                log.warning(
                    "ssh_command: invalid allowed_host_pattern %r — skipping", raw_pattern
                )

        # 2. CIDR validation — requires DNS resolution
        cidrs: list[str] = config.get("allowed_cidrs", [])
        if cidrs:
            try:
                loop = asyncio.get_event_loop()
                infos = await loop.run_in_executor(
                    None,
                    socket.getaddrinfo,
                    hostname,
                    None,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                )
                resolved_ip: str = str(infos[0][4][0])
            except OSError as exc:
                return None, (
                    f"Host {hostname!r} could not be resolved: {exc}. "
                    "Ensure the hostname is reachable from the container."
                )

            try:
                ip_obj = ipaddress.ip_address(resolved_ip)
            except ValueError:
                return None, (
                    f"Resolved address {resolved_ip!r} for host {hostname!r} "
                    "is not a valid IP address."
                )

            for cidr in cidrs:
                try:
                    network = ipaddress.ip_network(cidr, strict=False)
                    if ip_obj in network:
                        # Return resolved IP — prevents DNS rebinding on the actual connection
                        return resolved_ip, None
                except ValueError:
                    log.warning(
                        "ssh_command: invalid CIDR %r in allowed_cidrs — skipping", cidr
                    )

        if not patterns and not cidrs:
            return None, (
                "Dynamic host access is not configured. "
                "Add allowed_host_patterns or allowed_cidrs to the tool configuration, "
                "or use a configured host_key instead."
            )

        return None, (
            f"Host {hostname!r} is not allowed. "
            "It did not match any allowed_host_patterns and its resolved IP "
            "is not within any allowed_cidrs."
        )

    async def _resolve_target(
        self,
        config: dict,
        host_key: str,
        hostname: str,
        credential_key: str,
    ) -> tuple[Optional[dict], Optional[str], Optional[str]]:
        """Resolve the target host using either a fixed host_key or dynamic hostname.

        For fixed hosts (``host_key`` provided): returns the configured host dict and
        ``connect_hostname=None`` (asyncssh uses host dict's ``hostname`` directly).

        For dynamic hosts (``hostname`` + ``credential_key``): validates the hostname,
        builds a synthetic host dict from the credential profile, and returns the
        pre-resolved IP as ``connect_hostname`` to prevent DNS rebinding.

        Returns:
            ``(host_dict, connect_hostname, None)`` on success.
            ``(None, None, error_message)`` on failure.
        """
        if host_key:
            host, err = self._resolve_host(config, host_key)
            if err:
                return None, None, err
            return host, None, None

        # Dynamic path
        connect_hostname, err = await self._validate_dynamic_host(config, hostname)
        if err:
            return None, None, err

        cred, err = self._resolve_credential(config, credential_key)
        if err:
            return None, None, err
        assert cred is not None

        host_dict: dict = {
            "key": hostname,
            "hostname": hostname,
            "port": cred.get("port", 22),
            "username": cred["username"],
            "auth_method": cred["auth_method"],
            "password": cred.get("password"),
            "private_key_path": cred.get("private_key_path"),
            "private_key_b64": cred.get("private_key_b64"),
            "private_key_passphrase": cred.get("private_key_passphrase"),
        }
        return host_dict, connect_hostname, None

    def _security_check(
        self, command: str, config: dict
    ) -> Optional[str]:
        """Run all deny checks against *command*.

        Checks (in order):
        1. Hardcoded deny list (always enforced)
        2. Operator-configured denied_command_patterns

        Returns an error message string if blocked, ``None`` if allowed.
        """
        err = check_hardcoded_deny(command)
        if err:
            return err
        denied = config.get("denied_command_patterns", [])
        return check_config_deny(command, denied)

    async def _run_ssh(
        self,
        host: dict,
        command: str,
        config: dict,
        connect_hostname: Optional[str] = None,
    ) -> tuple[Optional[SSHCommandResult], Optional[str]]:
        """Execute *command* on the remote host described by *host* config dict.

        Args:
            host: Host config dict (fixed or synthetic from credential profile).
            command: Shell command to execute.
            config: Tool configuration dict.
            connect_hostname: Optional pre-resolved IP to use for the TCP connection
                instead of ``host["hostname"]``. Prevents DNS rebinding for dynamic hosts.

        Returns:
            ``(result, None)`` on success, ``(None, error_message)`` on failure.
        """
        try:
            result = await run_ssh_command(
                hostname=host["hostname"],
                connect_hostname=connect_hostname,
                port=int(host.get("port", 22)),
                username=host["username"],
                auth_method=host["auth_method"],
                password=host.get("password"),
                private_key_path=host.get("private_key_path"),
                private_key_b64=host.get("private_key_b64"),
                private_key_passphrase=host.get("private_key_passphrase"),
                command=command,
                connect_timeout=int(config.get("connection_timeout_seconds", 10)),
                command_timeout=int(config.get("command_timeout_seconds", 30)),
                max_output_bytes=int(config.get("max_output_bytes", 65_536)),
            )
        except TimeoutError as exc:
            return None, str(exc)
        except ImportError as exc:
            return None, str(exc)
        except Exception as exc:  # asyncssh errors, auth failures, etc.
            log.warning(
                "ssh_command: SSH connection/execution error for host %r: %s",
                host.get("key"),
                exc,
            )
            return None, f"SSH error: {exc}"
        return result, None

    @staticmethod
    def _build_output(
        host_key: str,
        command: str,
        result: SSHCommandResult,
        elapsed_ms: int,
    ) -> dict:
        """Build the ToolResult data payload from an SSHCommandResult."""
        return {
            "host_key": host_key,
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "truncated": result.truncated,
            "execution_time_ms": elapsed_ms,
        }
