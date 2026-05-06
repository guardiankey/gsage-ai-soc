"""gSage AI — WhatWeb Web Technology Fingerprinting tool."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from typing import ClassVar, Optional
from urllib.parse import urlparse

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Default Chrome user-agent — avoids blocks and returns real-world representations
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Comma-separated CIDRs to block (override via WHATWEB_BLOCKED_NETWORKS env var).
# Default: loopback + link-local + RFC-1918 private ranges + Docker networks.
_DEFAULT_BLOCKED_NETWORKS = (
    "127.0.0.0/8,::1/128,"
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "169.254.0.0/16,fe80::/10,"
    "fc00::/7"
)

# Hard cap on number of URLs per scan invocation
_MAX_TARGETS = 10

# Hard cap on technologies listed per URL in the result
_MAX_TECHNOLOGIES_PER_URL = 50

# Characters illegal in --plugins values (prevent argument injection)
_PLUGINS_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_,\-]")


# ── SSRF protection helpers ───────────────────────────────────────────────────

def _get_blocked_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Return the blocked network list from WHATWEB_BLOCKED_NETWORKS env var."""
    raw = os.environ.get("WHATWEB_BLOCKED_NETWORKS", _DEFAULT_BLOCKED_NETWORKS)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning(
                "whatweb_scan: ignoring invalid WHATWEB_BLOCKED_NETWORKS entry: %r",
                entry,
            )
    return networks


def _is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* falls within any blocked network."""
    for net in _get_blocked_networks():
        try:
            if addr in net:
                return True
        except TypeError:
            # IP version mismatch — skip
            continue
    return False


def _resolve_and_check(hostname: str) -> Optional[str]:
    """Resolve *hostname* to all IPs and check each against blocked networks.

    Returns an error message if the hostname resolves to a blocked range,
    or None if the hostname is safe.
    """
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return f"DNS resolution failed for '{hostname}': {exc}"

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        raw_ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if _is_blocked(addr):
            return (
                f"Target hostname '{hostname}' resolves to a blocked address "
                f"({raw_ip}). Internal/private network addresses are not permitted."
            )
    return None


def _validate_url(raw: str) -> tuple[str, Optional[str]]:
    """Validate and normalise a single URL target.

    Returns:
        (normalised_url, error_message_or_None)
    """
    url = raw.strip()
    if not url:
        return "", "URL cannot be empty."

    try:
        parsed = urlparse(url)
    except Exception:
        return "", f"Malformed URL: {url!r}"

    # Scheme must be http or https
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "", (
            f"Unsupported URL scheme: {url!r}. "
            "Only 'http://' and 'https://' targets are allowed."
        )

    hostname = parsed.hostname
    if not hostname:
        return "", f"URL has no hostname: {url!r}"

    # Check if the hostname is a literal IP address
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_blocked(addr):
            return "", (
                f"Target '{hostname}' is a blocked address "
                "(loopback, private, or Docker network)."
            )
        # Literal IP — no DNS resolution needed
        return url, None
    except ValueError:
        pass  # Not a literal IP — proceed to DNS resolution

    # Resolve hostname to IP(s) and check each against blocked networks
    dns_error = _resolve_and_check(hostname)
    if dns_error:
        return "", dns_error

    return url, None


def _validate_targets(
    raw_targets: list,
) -> tuple[list[str], Optional[str]]:
    """Validate a list of URL targets.

    Returns:
        (valid_urls, first_error_or_None)
    """
    if not isinstance(raw_targets, list) or len(raw_targets) == 0:
        return [], "'targets' must be a non-empty list of URLs."

    if len(raw_targets) > _MAX_TARGETS:
        return [], (
            f"Too many targets: {len(raw_targets)}. "
            f"Maximum allowed per scan is {_MAX_TARGETS}."
        )

    valid: list[str] = []
    for item in raw_targets:
        if not isinstance(item, str):
            return [], f"Each target must be a string. Got: {type(item).__name__!r}"
        norm, err = _validate_url(item)
        if err:
            return [], err
        valid.append(norm)

    return valid, None


# ── WhatWeb JSON output parser ────────────────────────────────────────────────

def _extract_plugin_detail(plugin_data: dict) -> Optional[str]:
    """Extract a short human-readable detail string from a WhatWeb plugin entry."""
    # WhatWeb plugins may carry: string[], version[], os[], account[], etc.
    parts: list[str] = []
    for field in ("string", "os", "account", "email", "country", "module"):
        values = plugin_data.get(field)
        if isinstance(values, list) and values:
            parts.extend(str(v) for v in values[:3])
    return (", ".join(parts) or None) if parts else None


def _extract_version(plugin_data: dict) -> Optional[str]:
    """Extract the first detected version string from a WhatWeb plugin entry."""
    versions = plugin_data.get("version")
    if isinstance(versions, list) and versions:
        first = versions[0]
        if isinstance(first, dict):
            return first.get("string") or first.get("version")
        if isinstance(first, str):
            return first
    return None


def _parse_whatweb_json(raw_output: str) -> list[dict]:
    """Parse WhatWeb JSON output (NDJSON or single array) into structured results.

    WhatWeb --log-json=- may emit:
      - NDJSON: one JSON object per line, one line per target
      - Single JSON array: older versions wrap all results in []

    Returns a list of simplified target dicts.
    """
    if not raw_output.strip():
        return []

    raw_output = raw_output.strip()

    # Try to detect JSON array (older format)
    if raw_output.startswith("["):
        try:
            entries = json.loads(raw_output)
            if isinstance(entries, list):
                pass  # use as-is
            else:
                entries = [entries]
        except json.JSONDecodeError:
            entries = []
    else:
        # NDJSON: one object per line
        entries = []
        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("whatweb_scan: skipping non-JSON line: %r", line[:120])

    results: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        target_url: str = entry.get("target", "")
        http_status: Optional[int] = entry.get("http_status")
        plugins: dict = entry.get("plugins") or {}

        # Build flattened technologies list
        technologies: list[dict] = []
        for plugin_name, plugin_data in plugins.items():
            if not isinstance(plugin_data, dict):
                continue
            tech: dict = {"name": plugin_name}
            ver = _extract_version(plugin_data)
            if ver:
                tech["version"] = ver
            detail = _extract_plugin_detail(plugin_data)
            if detail:
                tech["detail"] = detail
            technologies.append(tech)

        # Sort alphabetically and cap
        technologies.sort(key=lambda t: t["name"].lower())
        technologies = technologies[:_MAX_TECHNOLOGIES_PER_URL]

        # Determine final URL after redirects (WhatWeb may include redirect chain)
        redirected_to: Optional[str] = None
        redirect_chain = entry.get("redirect_chain") or []
        if isinstance(redirect_chain, list) and redirect_chain:
            last = redirect_chain[-1]
            if isinstance(last, str) and last != target_url:
                redirected_to = last

        results.append({
            "url": target_url,
            "http_status": http_status,
            "technologies": technologies,
            "technologies_count": len(technologies),
            **({"redirected_to": redirected_to} if redirected_to else {}),
        })

    return results


# ── Tool class ────────────────────────────────────────────────────────────────

class WhatwebScanTool(BaseTool):
    """Web technology fingerprinting via WhatWeb.

    Identifies web technologies on one or more targets: CMS (WordPress, Drupal,
    Joomla), frameworks, server software, JavaScript libraries, analytics
    trackers, CDNs, WAFs, and hundreds of other plugins.

    Security restrictions:
        - Only http:// and https:// URLs are accepted.
        - Target hostnames are resolved pre-scan; addresses in private/internal
          ranges (RFC 1918, loopback, link-local, Docker networks) are blocked
          to prevent SSRF.
        - Maximum 10 URLs per invocation.

    Aggression levels:
        - 1 (stealthy, default): A single HTTP request per target. Fast and
          low-visibility; suitable for most reconnaissance tasks.
        - 3 (aggressive): Makes additional requests per detected plugin to
          confirm and refine detections. More thorough but noisier.

    Execution:
        - Always runs in background (Celery). Requires human approval (HITL).
        - timeout: 600 s · rate limit: 10/minute

    Required parameter:
        targets (list[str]): One or more URLs (max 10).

    Permission: ``network:scan``
    """

    name: ClassVar[str] = "whatweb_scan"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Web technology fingerprinting via WhatWeb: CMS, frameworks, server software, and headers"
    category: ClassVar[str] = "network"
    permissions: ClassVar[list[str]] = ["network:scan"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 600
    use_circuit_breaker: ClassVar[bool] = False
    always_background: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "targets"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["targets"],
        "properties": {
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 10,
                "description": (
                    "List of target URLs to fingerprint (max 10). "
                    "Must use http:// or https:// scheme. "
                    "Examples: ['https://example.com', 'https://shop.example.com']"
                ),
            },
            "aggression": {
                "type": "string",
                "enum": ["1", "3"],
                "description": (
                    "Scan aggression level. "
                    "1 (default): stealthy — one HTTP request per target, low visibility. "
                    "3: aggressive — additional requests per plugin for better accuracy."
                ),
                "default": 1,
            },
            "max_redirects": {
                "type": "integer",
                "description": (
                    "Maximum number of HTTP redirects to follow per target (default: 5)."
                ),
                "default": 5,
                "minimum": 0,
                "maximum": 20,
            },
            "user_agent": {
                "type": "string",
                "description": (
                    "Custom HTTP User-Agent header. "
                    f"Defaults to Chrome 124: '{_DEFAULT_USER_AGENT}'"
                ),
            },
            "plugins": {
                "type": "string",
                "description": (
                    "Comma-separated list of WhatWeb plugin names to run "
                    "(e.g. 'WordPress,jQuery,PHP'). "
                    "Runs all plugins when omitted. "
                    "Only alphanumeric characters, hyphens, underscores, and commas allowed."
                ),
                "pattern": r"^[a-zA-Z0-9_,\-]+$",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Core execution ────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.monotonic()

        # ── Validate targets ──────────────────────────────────────────────
        raw_targets = params.get("targets", [])
        targets, validation_error = _validate_targets(raw_targets)
        if validation_error:
            return self._failure("INVALID_INPUT", validation_error)

        # ── Extract parameters ────────────────────────────────────────────
        aggression: int = int(params.get("aggression", 1))
        if aggression not in (1, 3):
            return self._failure(
                "INVALID_INPUT",
                f"'aggression' must be 1 (stealthy) or 3 (aggressive). Got: {aggression}",
            )

        max_redirects: int = min(int(params.get("max_redirects", 5)), 20)
        user_agent: str = params.get("user_agent") or _DEFAULT_USER_AGENT

        plugins_raw: Optional[str] = params.get("plugins") or None
        if plugins_raw is not None:
            # Sanitise: allow only safe characters (prevents argument injection)
            if _PLUGINS_UNSAFE_RE.search(plugins_raw):
                return self._failure(
                    "INVALID_INPUT",
                    "'plugins' contains invalid characters. "
                    "Only alphanumeric characters, hyphens, underscores, and commas are allowed.",
                )

        # ── Build WhatWeb command ─────────────────────────────────────────
        # --log-json=-  → write JSON output to stdout
        # Never use shell=True; each argument is a separate list element
        cmd: list[str] = [
            "whatweb",
            f"--aggression={aggression}",
            f"--max-redirects={max_redirects}",
            f"--user-agent={user_agent}",
            "--log-json=-",
            "--quiet",
        ]

        if plugins_raw:
            cmd.append(f"--plugins={plugins_raw}")

        # Append URLs last
        cmd.extend(targets)

        logger.info(
            "whatweb_scan: starting scan, %d target(s), aggression=%d, org=%s",
            len(targets),
            aggression,
            agent_context.org_id,
        )

        # ── Execute subprocess ────────────────────────────────────────────
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
                timeout=self.timeout_seconds - 5,  # reserve 5 s for post-processing
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            elapsed = int((time.monotonic() - start) * 1000)
            return self._failure(
                "SCAN_TIMEOUT",
                f"WhatWeb scan timed out after {self.timeout_seconds - 5}s. "
                "Consider using aggression=1 or reducing the number of targets.",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except FileNotFoundError:
            elapsed = int((time.monotonic() - start) * 1000)
            return self._failure(
                "WHATWEB_NOT_FOUND",
                "WhatWeb binary not found. Ensure whatweb is installed in the container.",
                retryable=False,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            return self._failure(
                "SCAN_ERROR",
                f"WhatWeb subprocess error: {exc}",
                retryable=False,
                execution_time_ms=elapsed,
            )

        json_output = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if proc is not None and proc.returncode not in (0, 1):
            # WhatWeb exits 1 on connection errors; other codes are unexpected
            logger.warning(
                "whatweb_scan: exit code %d for targets=%r: %s",
                proc.returncode,
                targets,
                stderr_text[:500],
            )

        # ── Parse JSON output ─────────────────────────────────────────────
        scan_results = _parse_whatweb_json(json_output)

        if not scan_results and json_output.strip():
            logger.warning(
                "whatweb_scan: produced output but parser returned empty list. "
                "stdout preview: %r",
                json_output[:500],
            )

        # ── Build summary ─────────────────────────────────────────────────
        total_technologies = sum(r.get("technologies_count", 0) for r in scan_results)
        elapsed = int((time.monotonic() - start) * 1000)

        result_data: dict = {
            "summary": {
                "targets_requested": len(targets),
                "targets_scanned": len(scan_results),
                "total_technologies_detected": total_technologies,
                "aggression": aggression,
            },
            "targets": scan_results,
        }
        if stderr_text.strip():
            result_data["whatweb_stderr"] = stderr_text[:2000]

        return self._success(result_data, execution_time_ms=elapsed)
