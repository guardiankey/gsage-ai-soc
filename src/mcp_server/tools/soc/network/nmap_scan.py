"""gSage AI — Nmap Network Scanner tool."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Comma-separated CIDRs to block (override via env var NMAP_BLOCKED_NETWORKS).
# Default: loopback + Docker default bridge + Docker overlay range.
_DEFAULT_BLOCKED_NETWORKS = "127.0.0.0/8,::1/128,172.17.0.0/16,172.16.0.0/12"

# Valid hostname regex (RFC 952 / 1123)
_HOSTNAME_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)

# Scan mode definitions: mode_name → (extra_flags, description)
_SCAN_MODES: dict[str, tuple[list[str], str]] = {
    "quick": (
        ["-sT", "--top-ports", "100", "-T4"],
        "TCP connect scan on top 100 ports (fast)",
    ),
    "service": (
        ["-sT", "-sV", "--top-ports", "1000", "-T4"],
        "TCP connect + service version detection on top 1000 ports",
    ),
    "full": (
        ["-sT", "-sV", "-O", "--top-ports", "1000", "-T4"],
        "TCP connect + service version + OS detection on top 1000 ports (requires NET_RAW)",
    ),
}

# Max IPv4 prefix allowed for CIDR targets (/24 = 256 hosts)
_MAX_IPV4_CIDR_PREFIX = 24
# Max IPv6 prefix allowed for CIDR targets (/120 = 256 hosts)
_MAX_IPV6_CIDR_PREFIX = 120


# ── Helper functions ─────────────────────────────────────────────────────────


def _get_blocked_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Return blocked network list from NMAP_BLOCKED_NETWORKS env var."""
    raw = os.environ.get("NMAP_BLOCKED_NETWORKS", _DEFAULT_BLOCKED_NETWORKS)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning(
                "nmap_scan: ignoring invalid NMAP_BLOCKED_NETWORKS entry: %r", entry
            )
    return networks


def _is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* overlaps any blocked network."""
    for net in _get_blocked_networks():
        try:
            if addr in net:
                return True
        except TypeError:
            # IPv4 address vs IPv6 network comparison — skip
            continue
    return False


def _validate_target(
    raw: str,
) -> tuple[str, Optional[str]]:
    """Validate and normalise the scan target.

    Returns:
        (normalised_target, error_message_or_None)
    """
    target = raw.strip()
    if not target:
        return "", "'target' is required."

    # ── CIDR notation ───────────────────────────────────────────────────
    if "/" in target:
        try:
            net = ipaddress.ip_network(target, strict=False)
        except ValueError:
            return "", f"Invalid CIDR: {target!r}"

        if isinstance(net, ipaddress.IPv4Network) and net.prefixlen < _MAX_IPV4_CIDR_PREFIX:
            return "", (
                f"CIDR range too large: {target!r} (/{net.prefixlen}). "
                f"Maximum allowed is /{_MAX_IPV4_CIDR_PREFIX} (256 hosts)."
            )
        if isinstance(net, ipaddress.IPv6Network) and net.prefixlen < _MAX_IPV6_CIDR_PREFIX:
            return "", (
                f"IPv6 CIDR too large: {target!r} (/{net.prefixlen}). "
                f"Minimum prefix is /{_MAX_IPV6_CIDR_PREFIX}."
            )

        if _is_blocked(net.network_address):
            return "", f"Target network {target!r} is blocked (internal/Docker network)."

        return str(net), None

    # ── Single IP address ───────────────────────────────────────────────
    try:
        addr = ipaddress.ip_address(target)
        if _is_blocked(addr):
            return "", f"Target {target!r} is blocked (loopback or internal network)."
        return str(addr), None
    except ValueError:
        pass

    # ── Hostname (FQDN) ─────────────────────────────────────────────────
    if _HOSTNAME_RE.match(target):
        return target, None

    return "", (
        f"Invalid target: {target!r}. "
        "Must be an IPv4/IPv6 address, CIDR range (max /24), or fully-qualified hostname."
    )


def _parse_nmap_xml(xml_data: str) -> dict:
    """Parse nmap XML stdout into a JSON-serialisable summary dict.

    Uses stdlib ``xml.etree.ElementTree`` — no extra dependencies.
    """
    summary: dict = {
        "hosts_scanned": 0,
        "hosts_up": 0,
        "hosts_down": 0,
        "hosts": [],
    }

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        return {"parse_error": f"Invalid XML from nmap: {exc}", "raw_xml_stored": True}

    for host_elem in root.findall("host"):
        summary["hosts_scanned"] += 1

        # State
        status_elem = host_elem.find("status")
        state = status_elem.get("state", "unknown") if status_elem is not None else "unknown"
        if state == "up":
            summary["hosts_up"] += 1
        else:
            summary["hosts_down"] += 1

        # IP address
        ip = ""
        for addr_elem in host_elem.findall("address"):
            if addr_elem.get("addrtype", "") in ("ipv4", "ipv6"):
                ip = addr_elem.get("addr", "")
                break

        # Hostname
        hostname_str = ""
        hostnames_elem = host_elem.find("hostnames")
        if hostnames_elem is not None:
            hn_elem = hostnames_elem.find("hostname")
            if hn_elem is not None:
                hostname_str = hn_elem.get("name", "")

        # Ports
        ports_list: list[dict] = []
        ports_elem = host_elem.find("ports")
        if ports_elem is not None:
            for port_elem in ports_elem.findall("port"):
                port_num = int(port_elem.get("portid", 0))
                protocol = port_elem.get("protocol", "tcp")

                port_state = ""
                state_elem = port_elem.find("state")
                if state_elem is not None:
                    port_state = state_elem.get("state", "")

                service_name = product = version = ""
                svc_elem = port_elem.find("service")
                if svc_elem is not None:
                    service_name = svc_elem.get("name", "")
                    product = svc_elem.get("product", "")
                    version = svc_elem.get("version", "")

                ports_list.append(
                    {
                        "port": port_num,
                        "protocol": protocol,
                        "state": port_state,
                        "service": service_name,
                        "product": product,
                        "version": version,
                    }
                )

        # OS matches (full mode)
        os_matches: list[dict] = []
        os_elem = host_elem.find("os")
        if os_elem is not None:
            for osmatch_elem in list(os_elem.findall("osmatch"))[:3]:
                os_matches.append(
                    {
                        "name": osmatch_elem.get("name", ""),
                        "accuracy": osmatch_elem.get("accuracy", ""),
                    }
                )

        summary["hosts"].append(
            {
                "ip": ip,
                "hostname": hostname_str,
                "state": state,
                "open_ports": [p for p in ports_list if p["state"] == "open"],
                "all_ports": ports_list,
                "os_matches": os_matches,
            }
        )

    return summary


# ── Tool class ───────────────────────────────────────────────────────────────


class NmapScanTool(BaseTool):
    """
    Nmap Network Scanner — perform TCP port scan and service detection.

    Use this tool to scan an IP address, CIDR range (max /24), or hostname
    for open ports, running services, and (optionally) OS fingerprinting.

    Available scan modes (``scan_mode`` parameter):
        - ``quick``: Top 100 TCP ports, fast (-T4). Good for initial triage.
        - ``service``: Top 1000 TCP ports + service/version detection.
          Best for comprehensive host assessment.
        - ``full``: Top 1000 TCP ports + service/version + OS detection.
          Most thorough; requires NET_RAW container capability.

    Security restrictions:
        - Loopback addresses (127.x, ::1) are always blocked.
        - Docker internal networks (172.17.0.0/16, 172.16.0.0/12) are
          blocked by default. Override via NMAP_BLOCKED_NETWORKS env var
          (comma-separated CIDRs).
        - CIDR targets are limited to /24 (256 hosts) for IPv4 or /120 for IPv6.

    Execution:
        - Always runs in background (Celery). The tool returns immediately with
          a task_id; results arrive via the conversation notification channel.
        - Requires human approval (HITL) before execution.

    Required parameter:
        target (str): IP (e.g. "203.0.113.5"), CIDR (e.g. "203.0.113.0/24"),
                      or hostname (e.g. "example.com").

    Optional parameter:
        scan_mode (str): "quick" | "service" | "full". Default: "service".

    Permission: ``network:scan``
    Timeout: 300 s · Rate limit: 5/min · Always background · Requires approval
    """

    name: ClassVar[str] = "nmap_scan"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Network port scan and service detection using Nmap on a host, IP address, or CIDR range"
    category: ClassVar[str] = "network"
    core_tool: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["network:scan"]
    rate_limit_per_minute: ClassVar[int] = 5
    timeout_seconds: ClassVar[int] = 300
    use_circuit_breaker: ClassVar[bool] = False  # network scans must not trip CB
    always_background: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "target"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["target"],
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "IP address (e.g. '203.0.113.5'), CIDR range up to /24 "
                    "(e.g. '203.0.113.0/24'), or fully-qualified hostname "
                    "(e.g. 'example.com')."
                ),
            },
            "scan_mode": {
                "type": "string",
                "enum": ["quick", "service", "full"],
                "description": (
                    "Scan depth to use. 'quick': top 100 ports, fast. "
                    "'service': top 1000 ports + version detection (default). "
                    "'full': top 1000 ports + version + OS detection."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        """
        Run nmap scan and return a JSON summary + stored XML file.

        Parameters
        ----------
        params:
            target (str, required): IP, CIDR, or hostname.
            scan_mode (str, optional): "quick" | "service" | "full". Default: "service".

        Returns
        -------
        ToolResult with data containing:
            summary (dict): Parsed scan results (hosts, ports, OS matches).
            file (dict | None): File store info for the raw XML report.
            nmap_stderr (str | None): nmap warning output (if any).
        """
        start = time.monotonic()

        # ── Input validation ──────────────────────────────────────────────
        raw_target = params.get("target", "")
        if not isinstance(raw_target, str):
            return self._failure("INVALID_INPUT", "'target' must be a string")

        target, validation_error = _validate_target(raw_target)
        if validation_error:
            return self._failure("INVALID_INPUT", validation_error)

        raw_mode = params.get("scan_mode", "service")
        if not isinstance(raw_mode, str) or raw_mode not in _SCAN_MODES:
            return self._failure(
                "INVALID_INPUT",
                f"'scan_mode' must be one of: {', '.join(_SCAN_MODES)}. Got: {raw_mode!r}",
            )

        scan_flags, mode_desc = _SCAN_MODES[raw_mode]

        # ── Build nmap command (never use shell=True) ─────────────────────
        # -oX - sends XML output to stdout for capture
        cmd = ["nmap"] + scan_flags + ["-oX", "-", target]

        logger.info(
            "nmap_scan: starting %s scan, target=%r, org=%s",
            raw_mode,
            target,
            agent_context.org_id,
        )

        # ── Execute nmap subprocess ───────────────────────────────────────
        proc: Optional[asyncio.subprocess.Process] = None
        stdout_bytes = b""
        stderr_bytes = b""

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout_seconds - 10,  # reserve 10s for post-processing
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            elapsed = int((time.monotonic() - start) * 1000)
            return self._failure(
                "SCAN_TIMEOUT",
                f"nmap scan timed out after {self.timeout_seconds - 10}s for target {target!r}. "
                "Consider using 'quick' mode or a smaller target.",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except FileNotFoundError:
            elapsed = int((time.monotonic() - start) * 1000)
            return self._failure(
                "NMAP_NOT_FOUND",
                "nmap binary not found. Ensure nmap is installed in the container.",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            return self._failure(
                "SCAN_ERROR",
                f"nmap subprocess error: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )

        xml_data = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if proc is not None and proc.returncode != 0:
            logger.warning(
                "nmap_scan: non-zero exit %d for target=%r: %s",
                proc.returncode,
                target,
                stderr_text[:500],
            )

        # ── Parse XML → JSON summary ──────────────────────────────────────
        summary = _parse_nmap_xml(xml_data)
        summary["scan_mode"] = raw_mode
        summary["mode_description"] = mode_desc
        summary["target"] = target
        summary["nmap_command"] = " ".join(cmd)

        # ── Store XML report in file store ────────────────────────────────
        file_info: Optional[dict] = None
        xml_bytes = xml_data.encode("utf-8")
        if xml_bytes:
            safe_target = re.sub(r"[^a-zA-Z0-9._-]", "_", target)
            filename = f"nmap_{safe_target}_{raw_mode}.xml"
            try:
                from src.shared.database import _get_session_maker  # noqa: PLC0415

                async with _get_session_maker()() as db_session:
                    file_info = await self._store_file(
                        data=xml_bytes,
                        filename=filename,
                        content_type="application/xml",
                        agent_context=agent_context,
                        session=db_session,
                        description=f"Nmap {raw_mode} scan XML report for {target}",
                    )
            except Exception as exc:
                logger.error(
                    "nmap_scan: failed to store XML report for target=%r: %s",
                    target,
                    exc,
                )

        # ── Build result ──────────────────────────────────────────────────
        elapsed = int((time.monotonic() - start) * 1000)

        result_data: dict = {"summary": summary}
        if file_info:
            result_data["file"] = file_info
        if stderr_text.strip():
            result_data["nmap_stderr"] = stderr_text[:2000]

        return self._success(result_data, execution_time_ms=elapsed)
