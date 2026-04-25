"""gSage AI — PCAP Analyzer tool.

Analyzes PCAP (packet capture) files uploaded as chat attachments.
Uses tshark for deep packet dissection and supports:
  - overview:  aggregate statistics (protocol distribution, top IPs/ports, flows,
               DNS queries, HTTP requests, TLS ClientHello SNI)
  - filter:    packet-level inspection with Wireshark display filter or BPF filter
  - flows:     TCP/UDP conversation tracking with byte counts
  - security:  heuristic anomaly detection (port scans, SYN floods, DNS exfiltration,
               ARP spoofing, ICMP tunneling)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections import Counter
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max bytes to load from MinIO for PCAP analysis (50 MB)
_PCAP_MAX_BYTES = 50 * 1024 * 1024

# Accepted PCAP MIME types (content_type stored in DB may vary by upload client)
_PCAP_CONTENT_TYPES = {
    "application/vnd.tcpdump.pcap",
    "application/x-pcap",
    "application/pcap",
    "application/octet-stream",
    "application/x-pcapng",
}

# Accepted file extensions
_PCAP_EXTENSIONS = {".pcap", ".pcapng"}

# Top-N entries returned per category in overview mode
_TOP_N = 20

# Max result size to inline in ToolResult before offloading to MinIO (50 KB)
_MAX_INLINE_BYTES = 50 * 1024

# Limit on display_filter / bpf_filter length to prevent abuse
_MAX_FILTER_LEN = 512

# Characters that must not appear in tshark/BPF filters to prevent shell injection.
# tshark is invoked via exec (no shell), so < > ! | are safe Wireshark syntax
# characters. We still block ; & backtick $ \ { } as defense-in-depth.
_FILTER_UNSAFE_RE = re.compile(r"[;&\x60$\\{}]")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_filter(value: str, field_name: str) -> Optional[str]:
    """Return an error message if *value* is not a safe filter string, or None."""
    if not isinstance(value, str):
        return f"'{field_name}' must be a string."
    if len(value) > _MAX_FILTER_LEN:
        return f"'{field_name}' exceeds maximum length of {_MAX_FILTER_LEN} characters."
    if _FILTER_UNSAFE_RE.search(value):
        return (
            f"'{field_name}' contains unsafe characters. "
            r"Blocked characters: ; & ` $ \ { }"
        )
    return None


def _is_pcap_file(filename: str, content_type: str) -> bool:
    """Return True if the file appears to be a PCAP or PCAPng."""
    suffix = os.path.splitext(filename.lower())[1]
    return suffix in _PCAP_EXTENSIONS or content_type.lower() in _PCAP_CONTENT_TYPES


def _tshark_path() -> str:
    """Return the tshark binary path (env override or default)."""
    return os.environ.get("TSHARK_PATH", "tshark")


# Stderr lines emitted by tshark that are cosmetic/informational and should be
# suppressed before surfacing errors to callers.
_TSHARK_STDERR_NOISE_RE = re.compile(
    r"^Running as user .* This could be dangerous\.?$",
    re.IGNORECASE,
)


def _clean_tshark_stderr(raw: str) -> str:
    """Strip known cosmetic tshark stderr noise (e.g. root-user warnings)."""
    lines = [ln for ln in raw.splitlines() if not _TSHARK_STDERR_NOISE_RE.match(ln.strip())]
    return "\n".join(lines).strip()


async def _run_tshark(args: list[str], timeout: float = 90.0) -> tuple[str, str, int]:
    """Run tshark with *args* (never via shell) and return (stdout, stderr, returncode)."""
    cmd = [_tshark_path()] + args
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return (
            stdout_bytes.decode("utf-8", errors="replace"),
            _clean_tshark_stderr(stderr_bytes.decode("utf-8", errors="replace")),
            proc.returncode or 0,
        )
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        return "", "tshark timed out", -1
    except FileNotFoundError:
        return "", "tshark binary not found", -2


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Sankey helpers — produce cycle-free, Mermaid-safe flow data from pcap output
# ---------------------------------------------------------------------------

# Max nodes (pairs) to include in the sankey hint to keep the diagram readable.
_SANKEY_MAX_PAIRS = 25


def _sanitize_sankey_node(name: str) -> str:
    """Return a Mermaid sankey-safe node label.

    Sankey uses CSV parsing; colons, commas and double-quotes break the parser,
    and the label must stay ASCII-friendly. IPv4 dots are preserved.
    """
    if not name:
        return "unknown"
    cleaned = name.strip()
    # IPv6 and port separators: replace ':' with '-'
    cleaned = cleaned.replace(":", "-")
    # Commas and quotes would need escaping; replace with '-'
    cleaned = cleaned.replace(",", "-").replace('"', "").replace("'", "")
    # Collapse repeated hyphens (e.g. from '::') and trim
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "unknown"


def _build_directed_flows(
    pair_bytes: dict[tuple[str, str], int],
    max_pairs: int = _SANKEY_MAX_PAIRS,
) -> list[dict]:
    """Collapse a bidirectional ``{(src, dst): bytes}`` map into cycle-free flows.

    For each unordered pair ``{A, B}`` only the dominant direction (higher byte
    count) is kept; the reverse flow is merged into the dominant one. Node
    names are sanitized for Mermaid sankey (no colons/commas/quotes).

    Returns a list of ``{"source", "target", "value"}`` entries sorted by value
    descending and truncated to ``max_pairs``.
    """
    merged: dict[tuple[str, str], int] = {}
    for (src, dst), value in pair_bytes.items():
        if not src or not dst or src == dst or value <= 0:
            continue
        s = _sanitize_sankey_node(src)
        d = _sanitize_sankey_node(dst)
        if s == d:
            continue
        # Check if reverse direction already recorded
        reverse = (d, s)
        forward = (s, d)
        if reverse in merged:
            if value > merged[reverse]:
                # New direction dominates — drop reverse, keep combined value
                merged[forward] = value + merged.pop(reverse)
            else:
                merged[reverse] += value
        else:
            merged[forward] = merged.get(forward, 0) + value

    flows = [
        {"source": s, "target": d, "value": v}
        for (s, d), v in merged.items()
    ]
    flows.sort(key=lambda f: f["value"], reverse=True)
    return flows[:max_pairs]


def _flows_to_sankey_csv(directed_flows: list[dict]) -> str:
    """Render a list of directed flows as a Mermaid ``sankey-beta`` CSV block.

    Includes a frontmatter config block that hides per-link numeric labels
    (``showValues: false``) — with dozens of flows and 6-digit byte counts
    the labels overlap and hurt readability. The agent can still describe
    the exact values from the ``directed_flows`` list in the narrative.
    """
    if not directed_flows:
        return ""
    lines = [
        "---",
        "config:",
        "  sankey:",
        "    showValues: false",
        "---",
        "sankey-beta",
    ]
    lines.extend(
        f"{f['source']},{f['target']},{f['value']}" for f in directed_flows
    )
    return "\n".join(lines)


def _build_sankey_hint(pair_bytes: dict[tuple[str, str], int]) -> Optional[dict]:
    """Build a sankey-ready payload for the agent.

    Returns ``None`` when there is no useful data. The payload includes:

    - ``directed_flows``: cycle-free list of ``{source, target, value}``
    - ``mermaid``: ready-to-paste ``sankey-beta`` CSV block
    - ``notes``: usage guidance (and tip to fall back to a flowchart)
    """
    flows = _build_directed_flows(pair_bytes)
    if not flows:
        return None
    return {
        "directed_flows": flows,
        "mermaid": _flows_to_sankey_csv(flows),
        "notes": (
            "Cycle-free sankey data ready to render. Use the 'mermaid' string "
            "as-is, or the 'directed_flows' list to customize. Bidirectional "
            "traffic has already been merged into the dominant direction per "
            "host pair, and IPv6 colons are replaced with '-'. Do NOT add a "
            "synthetic root node like 'Network Traffic' on top — it creates "
            "cycles when a host appears as both source and destination. For "
            "complex patterns with genuine cycles prefer a 'flowchart LR'."
        ),
    }


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------


async def _analyze_overview(pcap_path: str, limit: int) -> dict:  # noqa: ARG001
    """Return aggregate statistics for the PCAP file.

    Collects protocol distribution, top source/destination IPs and ports,
    and top conversations using multiple focused tshark passes.
    """
    result: dict = {}

    # ── 1. Protocol hierarchy (text output) ─────────────────────────────────
    stdout, stderr, rc = await _run_tshark([
        "-r", pcap_path,
        "-q",
        "-z", "io,phs",
    ])
    if rc == 0 and stdout:
        result["protocol_hierarchy_raw"] = stdout.strip()[:3000]

    # ── 2. Packet count and time range (one-line summary) ───────────────────
    stdout, stderr, rc = await _run_tshark([
        "-r", pcap_path,
        "-q",
        "-z", "io,stat,0",
    ])
    if rc == 0 and stdout:
        result["capture_summary_raw"] = stdout.strip()[:1000]

    # ── 3. Field extraction: src IP, dst IP, protocol, frame length ─────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-T", "fields",
        "-e", "frame.number",
        "-e", "frame.time_epoch",
        "-e", "ip.src",
        "-e", "ip.dst",
        "-e", "ipv6.src",
        "-e", "ipv6.dst",
        "-e", "_ws.col.Protocol",
        "-e", "frame.len",
        "-e", "tcp.srcport",
        "-e", "tcp.dstport",
        "-e", "udp.srcport",
        "-e", "udp.dstport",
        "-E", "header=n",
        "-E", "separator=\t",
    ])

    if rc == 0 and stdout:
        src_ip_ctr: Counter = Counter()
        dst_ip_ctr: Counter = Counter()
        proto_ctr: Counter = Counter()
        sport_ctr: Counter = Counter()
        dport_ctr: Counter = Counter()
        conversation_ctr: Counter = Counter()
        # (src_ip, dst_ip) → bytes — used to build a cycle-free sankey hint.
        pair_bytes_ctr: Counter = Counter()
        total_packets = 0
        total_bytes = 0
        first_ts: Optional[float] = None
        last_ts: Optional[float] = None

        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            _, ts, ip_src, ip_dst, ip6_src, ip6_dst, proto, frame_len = parts[:8]
            src_port = parts[8] if len(parts) > 8 else ""
            dst_port = parts[9] if len(parts) > 9 else ""
            if not src_port:
                src_port = parts[10] if len(parts) > 10 else ""
            if not dst_port:
                dst_port = parts[11] if len(parts) > 11 else ""

            src = ip_src or ip6_src
            dst = ip_dst or ip6_dst

            total_packets += 1
            total_bytes += _safe_int(frame_len)

            try:
                ts_f = float(ts)
                if first_ts is None or ts_f < first_ts:
                    first_ts = ts_f
                if last_ts is None or ts_f > last_ts:
                    last_ts = ts_f
            except (ValueError, TypeError):
                pass

            if src:
                src_ip_ctr[src] += 1
            if dst:
                dst_ip_ctr[dst] += 1
            if proto:
                proto_ctr[proto] += 1
            if src_port:
                sport_ctr[src_port] += 1
            if dst_port:
                dport_ctr[dst_port] += 1
            if src and dst:
                key = f"{src}:{src_port} → {dst}:{dst_port}" if src_port or dst_port else f"{src} → {dst}"
                conversation_ctr[key] += 1
                pair_bytes_ctr[(src, dst)] += _safe_int(frame_len)

        result["statistics"] = {
            "total_packets": total_packets,
            "total_bytes": total_bytes,
            "duration_seconds": round(last_ts - first_ts, 3) if first_ts and last_ts else None,
            "start_time_epoch": first_ts,
            "end_time_epoch": last_ts,
        }
        result["top_source_ips"] = [
            {"ip": ip, "packets": cnt}
            for ip, cnt in src_ip_ctr.most_common(_TOP_N)
        ]
        result["top_dest_ips"] = [
            {"ip": ip, "packets": cnt}
            for ip, cnt in dst_ip_ctr.most_common(_TOP_N)
        ]
        result["protocol_distribution"] = [
            {"protocol": p, "packets": cnt}
            for p, cnt in proto_ctr.most_common(_TOP_N)
        ]
        result["top_source_ports"] = [
            {"port": p, "packets": cnt}
            for p, cnt in sport_ctr.most_common(_TOP_N)
        ]
        result["top_dest_ports"] = [
            {"port": p, "packets": cnt}
            for p, cnt in dport_ctr.most_common(_TOP_N)
        ]
        result["top_conversations"] = [
            {"flow": flow, "packets": cnt}
            for flow, cnt in conversation_ctr.most_common(_TOP_N)
        ]

        sankey_hint = _build_sankey_hint(pair_bytes_ctr)
        if sankey_hint:
            result["sankey_hint"] = sankey_hint

    # ── 4. DNS queries (if present) ──────────────────────────────────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "dns.flags.response == 0",
        "-T", "fields",
        "-e", "dns.qry.name",
        "-e", "dns.qry.type",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    if rc == 0 and stdout:
        dns_ctr: Counter = Counter()
        dns_types: dict[str, str] = {}
        for line in stdout.splitlines():
            parts = line.split("\t")
            name = parts[0].strip() if parts else ""
            qtype = parts[1].strip() if len(parts) > 1 else ""
            if name:
                dns_ctr[name] += 1
                if qtype and name not in dns_types:
                    dns_types[name] = qtype
        result["top_dns_queries"] = [
            {"query": q, "count": cnt, "type": dns_types.get(q, "")}
            for q, cnt in dns_ctr.most_common(_TOP_N)
        ]

    # DNS responses: A/AAAA resolutions
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "dns.flags.response == 1",
        "-T", "fields",
        "-e", "dns.qry.name",
        "-e", "dns.a",
        "-e", "dns.aaaa",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    if rc == 0 and stdout:
        resolutions: dict[str, set] = {}
        for line in stdout.splitlines():
            parts = line.split("\t")
            name = parts[0].strip() if parts else ""
            a_rec = parts[1].strip() if len(parts) > 1 else ""
            aaaa_rec = parts[2].strip() if len(parts) > 2 else ""
            if not name:
                continue
            resolved = a_rec or aaaa_rec
            if resolved:
                resolutions.setdefault(name, set()).add(resolved)
        result["dns_resolutions"] = [
            {"query": name, "addresses": sorted(addrs)}
            for name, addrs in list(resolutions.items())[:_TOP_N]
        ]

    # ── 5. HTTP hosts + requests (if present) ─────────────────────────────────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "http.request",
        "-T", "fields",
        "-e", "http.host",
        "-e", "http.request.method",
        "-e", "http.request.uri",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    if rc == 0 and stdout:
        http_ctr: Counter = Counter()
        http_req_ctr: Counter = Counter()
        for line in stdout.splitlines():
            parts = line.split("\t")
            host = parts[0].strip() if parts else ""
            method = parts[1].strip() if len(parts) > 1 else ""
            uri = parts[2].strip() if len(parts) > 2 else ""
            if host:
                http_ctr[host] += 1
            if method and host:
                key = f"{method} {host}{uri[:80]}"
                http_req_ctr[key] += 1
        result["top_http_hosts"] = [
            {"host": h, "requests": cnt}
            for h, cnt in http_ctr.most_common(_TOP_N)
        ]
        if http_req_ctr:
            result["http_requests"] = [
                {"request": req, "count": cnt}
                for req, cnt in http_req_ctr.most_common(_TOP_N)
            ]

    # ── 6. TLS ClientHello (SNI + version) ────────────────────────────────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "tls.handshake.type==1",
        "-T", "fields",
        "-e", "tls.handshake.extensions_server_name",
        "-e", "tls.handshake.version",
        "-e", "tls.handshake.extensions.supported_versions",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    if rc == 0 and stdout:
        sni_ctr: Counter = Counter()
        tls_ver_ctr: Counter = Counter()
        for line in stdout.splitlines():
            parts = line.split("\t")
            sni = parts[0].strip() if parts else ""
            version = parts[1].strip() if len(parts) > 1 else ""
            sup_versions = parts[2].strip() if len(parts) > 2 else ""
            if sni:
                sni_ctr[sni] += 1
            ver_label = sup_versions or version
            if ver_label:
                tls_ver_ctr[ver_label] += 1
        if sni_ctr or tls_ver_ctr:
            result["tls_client_hellos"] = {
                "top_sni": [
                    {"sni": sni, "count": cnt}
                    for sni, cnt in sni_ctr.most_common(_TOP_N)
                ],
                "version_distribution": [
                    {"version": v, "count": cnt}
                    for v, cnt in tls_ver_ctr.most_common()
                ],
            }

    return result


async def _analyze_filter(
    pcap_path: str,
    display_filter: Optional[str],
    bpf_filter: Optional[str],
    limit: int,
) -> dict:
    """Return packet details for packets matching the given filter.

    Prefers display_filter (tshark -Y); falls back to bpf_filter (-f).
    Returns up to *limit* packets.
    """
    args = ["-r", pcap_path]

    if display_filter:
        args += ["-Y", display_filter]
    elif bpf_filter:
        args += ["-f", bpf_filter]

    args += [
        "-c", str(limit),
        "-T", "fields",
        "-e", "frame.number",
        "-e", "frame.time",
        "-e", "ip.src",
        "-e", "ip.dst",
        "-e", "ipv6.src",
        "-e", "ipv6.dst",
        "-e", "_ws.col.Protocol",
        "-e", "frame.len",
        "-e", "tcp.srcport",
        "-e", "tcp.dstport",
        "-e", "udp.srcport",
        "-e", "udp.dstport",
        "-e", "tcp.flags.str",
        "-e", "_ws.col.Info",
        "-E", "header=n",
        "-E", "separator=\t",
    ]

    stdout, stderr, rc = await _run_tshark(args)

    packets: list[dict] = []
    if rc == 0 and stdout:
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 8:
                continue

            num, ts, ip_src, ip_dst, ip6_src, ip6_dst, proto, frame_len = parts[:8]
            sport = parts[8] if len(parts) > 8 else ""
            dport = parts[9] if len(parts) > 9 else ""
            if not sport and len(parts) > 10:
                sport = parts[10]
            if not dport and len(parts) > 11:
                dport = parts[11]
            flags = parts[12] if len(parts) > 12 else ""
            info = parts[13] if len(parts) > 13 else ""

            pkt: dict = {
                "frame": _safe_int(num),
                "timestamp": ts.strip(),
                "src": ip_src or ip6_src,
                "dst": ip_dst or ip6_dst,
                "protocol": proto,
                "length": _safe_int(frame_len),
            }
            if sport:
                pkt["src_port"] = _safe_int(sport)
            if dport:
                pkt["dst_port"] = _safe_int(dport)
            if flags:
                pkt["tcp_flags"] = flags
            if info:
                pkt["info"] = info.strip()[:200]

            packets.append(pkt)

    result = {
        "filter_applied": display_filter or bpf_filter or "(none)",
        "filter_type": "display" if display_filter else ("bpf" if bpf_filter else "none"),
        "packets_returned": len(packets),
        "packets": packets,
    }
    if rc == -2:
        result["error"] = "tshark binary not found."
    elif rc != 0 and stderr:
        result["tshark_stderr"] = stderr.strip()[:500]

    return result


async def _analyze_flows(
    pcap_path: str,
    display_filter: Optional[str],
    limit: int,
) -> dict:
    """Return TCP and UDP conversation statistics.

    Uses tshark -z conv,tcp and -z conv,udp statistics.
    Optionally narrows scope with a display filter.
    """

    async def _conv_stats(proto: str) -> list[dict]:
        args = ["-r", pcap_path, "-q", "-z", f"conv,{proto}"]
        if display_filter:
            args += ["-2", "-R", display_filter]
        stdout, _, rc = await _run_tshark(args)
        flows: list[dict] = []
        if rc != 0 or not stdout:
            return flows

        # Parse the tabular output produced by tshark -z conv,*
        # tshark 4.x adds Relative Start and Duration columns at the end, which
        # breaks naive re.split("\ {2,}") positional parsing.
        # Strategy: split on " <-> " to isolate endpoints; then use re.findall
        # to pick up comma-free digit runs from the remainder, mapping positionally.
        in_table = False
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("==="):
                in_table = not in_table
                continue
            if not in_table or not stripped or stripped.startswith("<-"):
                continue
            # Skip header rows
            if stripped.lower().startswith(("filter:", "address", "<")):
                continue
            if " <-> " not in stripped:
                continue
            try:
                left, right_remainder = stripped.split(" <-> ", 1)
                src_ep = left.strip()
                # dst endpoint ends where the first run of whitespace+digits begins
                # Find the transition from endpoint chars to numeric columns
                m = re.search(r"\s{2,}", right_remainder)
                if m:
                    dst_ep = right_remainder[: m.start()].strip()
                    numeric_part = right_remainder[m.start():]
                else:
                    dst_ep = right_remainder.strip()
                    numeric_part = ""
                # Extract all comma-stripped numeric tokens in order
                # Positions: 0=frames_AB, 1=bytes_AB, 2=frames_BA, 3=bytes_BA,
                #            4=frames_total, 5=bytes_total
                # Positions 6,7 (tshark 4.x) = Rel Start, Duration — ignored.
                num_tokens = [
                    tok.replace(",", "")
                    for tok in re.findall(r"[\d,]+", numeric_part)
                ]
                if len(num_tokens) < 4:
                    continue
                flow: dict = {
                    "protocol": proto.upper(),
                    "src": src_ep,
                    "dst": dst_ep,
                    "frames_src_to_dst": _safe_int(num_tokens[0]),
                    "bytes_src_to_dst": _safe_int(num_tokens[1]),
                    "frames_dst_to_src": _safe_int(num_tokens[2]),
                    "bytes_dst_to_src": _safe_int(num_tokens[3]),
                }
                if len(num_tokens) >= 6:
                    flow["total_frames"] = _safe_int(num_tokens[4])
                    flow["total_bytes"] = _safe_int(num_tokens[5])
                else:
                    flow["total_bytes"] = flow["bytes_src_to_dst"] + flow["bytes_dst_to_src"]
                flows.append(flow)
            except Exception:
                continue

        # Sort by total_bytes descending
        flows.sort(key=lambda f: f.get("total_bytes", 0), reverse=True)
        return flows[:limit]

    tcp_flows, udp_flows = await asyncio.gather(
        _conv_stats("tcp"),
        _conv_stats("udp"),
    )

    # Build a cycle-free sankey hint from all conversations. Use IP-only
    # endpoints (drop :port) so that bidirectional traffic on a single host
    # pair collapses correctly into one dominant direction.
    def _endpoint_ip(endpoint: str) -> str:
        ep = (endpoint or "").strip()
        if not ep:
            return ""
        # IPv6 form from tshark: "[2804:d59::1]:443"
        if ep.startswith("["):
            closing = ep.find("]")
            if closing != -1:
                return ep[1:closing]
        # IPv4 form: "1.2.3.4:443" — only strip port if there's exactly one ':'
        if ep.count(":") == 1:
            return ep.rsplit(":", 1)[0]
        # Bare IPv6 without port, or unknown — keep as-is
        return ep

    pair_bytes_ctr: Counter = Counter()
    for flow in (*tcp_flows, *udp_flows):
        src_ip = _endpoint_ip(flow.get("src", ""))
        dst_ip = _endpoint_ip(flow.get("dst", ""))
        total = _safe_int(str(flow.get("total_bytes", 0)))
        if src_ip and dst_ip and total > 0:
            pair_bytes_ctr[(src_ip, dst_ip)] += total

    result = {
        "filter_applied": display_filter or "(none)",
        "tcp_conversations": tcp_flows,
        "tcp_total": len(tcp_flows),
        "udp_conversations": udp_flows,
        "udp_total": len(udp_flows),
    }
    sankey_hint = _build_sankey_hint(pair_bytes_ctr)
    if sankey_hint:
        result["sankey_hint"] = sankey_hint
    return result


# ---------------------------------------------------------------------------
# Security analysis
# ---------------------------------------------------------------------------


async def _analyze_security(pcap_path: str, limit: int) -> dict:  # noqa: ARG001
    """Run heuristic security analysis on a PCAP file.

    Checks for:
      - Port scan (one source probing many TCP ports with SYN-only packets)
      - SYN flood (many SYNs to one destination from many sources)
      - DNS exfiltration indicators (unusually long query labels or names)
      - ARP spoofing (same IP advertised by multiple MACs)
      - ICMP tunneling (oversized ICMP packets)

    Returns a dict with ``alerts`` list and ``statistics`` dict.
    """
    alerts: list[dict] = []

    # ── (a + b) Port scan & SYN flood — share one tshark pass ───────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "tcp.flags.syn==1 && tcp.flags.ack==0",
        "-T", "fields",
        "-e", "ip.src",
        "-e", "ip.dst",
        "-e", "tcp.dstport",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    if rc == 0 and stdout:
        # {src_ip: {dst_port: set(), ...}} and {dst_ip: {src_ip: count}}
        src_to_ports: dict[str, set] = {}
        dst_syn_count: dict[str, int] = {}
        dst_src_ips: dict[str, set] = {}

        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            src, dst, dport = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if not src or not dst or not dport:
                continue
            src_to_ports.setdefault(src, set()).add(dport)
            dst_syn_count[dst] = dst_syn_count.get(dst, 0) + 1
            dst_src_ips.setdefault(dst, set()).add(src)

        for src_ip, ports in src_to_ports.items():
            if len(ports) > 20:
                alerts.append({
                    "type": "port_scan",
                    "severity": "high",
                    "description": f"Host {src_ip} sent SYN packets to {len(ports)} unique destination ports.",
                    "evidence": {
                        "src_ip": src_ip,
                        "unique_dst_ports": len(ports),
                        "sample_ports": sorted(ports)[:10],
                    },
                })

        for dst_ip, count in dst_syn_count.items():
            src_count = len(dst_src_ips.get(dst_ip, set()))
            if count > 100 and src_count > 5:
                alerts.append({
                    "type": "syn_flood",
                    "severity": "high",
                    "description": (
                        f"Host {dst_ip} received {count} SYN-only packets "
                        f"from {src_count} source IPs — possible SYN flood."
                    ),
                    "evidence": {
                        "dst_ip": dst_ip,
                        "syn_count": count,
                        "unique_sources": src_count,
                    },
                })

    # ── (c) DNS exfiltration indicators ─────────────────────────────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "dns.flags.response==0",
        "-T", "fields",
        "-e", "dns.qry.name",
        "-E", "header=n",
        "-E", "separator=\n",
    ])
    if rc == 0 and stdout:
        for qname in stdout.splitlines():
            qname = qname.strip()
            if not qname:
                continue
            # Flag if any label exceeds 50 chars or total name exceeds 200 chars
            labels = qname.split(".")
            max_label = max((len(lbl) for lbl in labels), default=0)
            if max_label > 50 or len(qname) > 200:
                alerts.append({
                    "type": "dns_exfiltration",
                    "severity": "medium",
                    "description": (
                        f"Unusually long DNS query detected — possible DNS tunneling or exfiltration."
                    ),
                    "evidence": {
                        "query": qname[:200],
                        "total_length": len(qname),
                        "max_label_length": max_label,
                    },
                })

    # ── (d) ARP spoofing ─────────────────────────────────────────────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "arp.opcode==2",
        "-T", "fields",
        "-e", "arp.src.hw_mac",
        "-e", "arp.src.proto_ipv4",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    if rc == 0 and stdout:
        ip_to_macs: dict[str, set] = {}
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            mac, ip = parts[0].strip(), parts[1].strip()
            if mac and ip:
                ip_to_macs.setdefault(ip, set()).add(mac)
        for ip, macs in ip_to_macs.items():
            if len(macs) > 1:
                alerts.append({
                    "type": "arp_spoofing",
                    "severity": "high",
                    "description": (
                        f"IP {ip} announced by {len(macs)} different MAC addresses — possible ARP spoofing."
                    ),
                    "evidence": {
                        "ip": ip,
                        "mac_addresses": sorted(macs),
                    },
                })

    # ── (e) ICMP tunneling ───────────────────────────────────────────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-Y", "icmp",
        "-T", "fields",
        "-e", "ip.src",
        "-e", "ip.dst",
        "-e", "frame.len",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    if rc == 0 and stdout:
        large_icmp: list[dict] = []
        pair_sizes: dict[str, list] = {}
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            src, dst, flen = parts[0].strip(), parts[1].strip(), parts[2].strip()
            size = _safe_int(flen)
            if size > 1000:
                key = f"{src} → {dst}"
                pair_sizes.setdefault(key, []).append(size)
        for pair, sizes in pair_sizes.items():
            avg = sum(sizes) / len(sizes)
            large_icmp.append({
                "type": "icmp_tunnel",
                "severity": "medium",
                "description": (
                    f"Large ICMP packets detected ({len(sizes)} packets, avg {avg:.0f} bytes) "
                    f"on flow {pair} — possible ICMP tunneling."
                ),
                "evidence": {
                    "flow": pair,
                    "count": len(sizes),
                    "avg_size_bytes": round(avg),
                    "max_size_bytes": max(sizes),
                },
            })
        alerts.extend(large_icmp)

    # ── (f) Summary statistics ───────────────────────────────────────────────
    stdout, _, rc = await _run_tshark([
        "-r", pcap_path,
        "-T", "fields",
        "-e", "ip.src",
        "-e", "ip.dst",
        "-e", "tcp.dstport",
        "-e", "udp.dstport",
        "-E", "header=n",
        "-E", "separator=\t",
    ])
    statistics: dict = {}
    if rc == 0 and stdout:
        total = 0
        src_ips: set = set()
        dst_ips: set = set()
        dst_ports: set = set()
        for line in stdout.splitlines():
            parts = line.split("\t")
            total += 1
            src = parts[0].strip() if parts else ""
            dst = parts[1].strip() if len(parts) > 1 else ""
            tcp_dp = parts[2].strip() if len(parts) > 2 else ""
            udp_dp = parts[3].strip() if len(parts) > 3 else ""
            if src:
                src_ips.add(src)
            if dst:
                dst_ips.add(dst)
            dp = tcp_dp or udp_dp
            if dp:
                dst_ports.add(dp)
        statistics = {
            "total_packets": total,
            "unique_src_ips": len(src_ips),
            "unique_dst_ips": len(dst_ips),
            "unique_dst_ports": len(dst_ports),
        }

    return {
        "alerts": alerts,
        "total_alerts": len(alerts),
        "statistics": statistics,
    }


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class PcapAnalyzerTool(BaseTool):
    """
    PCAP Analyzer — analyze packet capture files attached to the conversation.

    Inspect network traffic captures (.pcap / .pcapng) using tshark.
    The file must be uploaded as a chat attachment first (use the attach
    command or the web UI file picker), then pass its ``file_id`` here.

    Available modes (``mode`` parameter):

        overview (default)
            Aggregate statistics: total packets, bytes, duration, protocol
            distribution, top source/destination IPs, top ports, DNS queries
            (with type and resolutions), HTTP requests, TLS ClientHello SNI,
            and top conversations.  Good starting point to orient the analysis.
            Also returns a ``sankey_hint`` with cycle-free, Mermaid-ready
            flow data — use it directly when generating sankey diagrams.

        filter
            Packet-level inspection.  Apply a Wireshark display filter
            (``display_filter``) or a BPF/tcpdump filter (``bpf_filter``)
            and get back the first ``limit`` matching packets with timestamp,
            src/dst IP, ports, protocol, length, TCP flags, and info summary.

            Examples:
                display_filter: "dns"
                display_filter: "tcp.port == 443 && ip.src == 10.0.0.1"
                bpf_filter:     "host 192.168.1.1 and port 80"

        flows
            TCP and UDP conversation tracking.  Returns a table of flows
            sorted by total bytes (most active first) with directional
            frame/byte counts.  Optionally narrow scope with ``display_filter``.
            Also returns a ``sankey_hint`` with cycle-free, Mermaid-ready
            flow data — use it directly when generating sankey diagrams.

        security
            Heuristic anomaly detection.  Scans for:
              - port_scan: one host probing many TCP ports via SYN-only packets
              - syn_flood: high-volume SYN traffic to a single destination
              - dns_exfiltration: unusually long DNS query labels or names
              - arp_spoofing: same IP address announced by multiple MACs
              - icmp_tunnel: oversized ICMP packets indicating tunneling
            Returns a list of ``alerts`` with severity, description, and
            evidence, plus a packet-level ``statistics`` summary.

    Execution notes:
        - Runs synchronously for small files; falls back to background (Celery)
          if execution exceeds ``background_threshold_seconds`` or file is large.
        - PCAP files up to 50 MB are supported.
        - Filter results > 50 KB are stored in the file store and returned
          as a download link rather than inline.

    Required parameter:
        file_id (str): UUID of the attached PCAP or PCAPng file.

    Optional parameters:
        mode (str): "overview" | "filter" | "flows" | "security". Default: "overview".
        display_filter (str): Wireshark display filter syntax.
        bpf_filter (str): BPF / tcpdump filter syntax.
        limit (int): Max packets or flows to return. Default: 100.

    Permission: ``network:analyze``
    """

    name: ClassVar[str] = "pcap_analyzer"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Analyze PCAP/PCAPng network captures: traffic overview, flow analysis, and security alerts"
    category: ClassVar[str] = "network"
    permissions: ClassVar[list[str]] = ["network:analyze"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 120
    background_threshold_seconds: ClassVar[Optional[int]] = 60
    requires_approval: ClassVar[bool] = False
    use_circuit_breaker: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {"target_entities": "file_id"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id"],
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the attached PCAP or PCAPng file to analyze. "
                    "The file must be uploaded as a chat attachment before calling this tool."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["overview", "filter", "flows", "security"],
                "description": (
                    "Analysis mode. "
                    "'overview': aggregate statistics — protocol distribution, top IPs/ports, DNS, HTTP, TLS SNI. "
                    "'filter': packet-level inspection with display or BPF filter. "
                    "'flows': TCP/UDP conversation table sorted by byte volume. "
                    "'security': heuristic anomaly detection (port scans, SYN floods, DNS exfiltration, ARP spoofing, ICMP tunneling)."
                ),
                "default": "overview",
            },
            "display_filter": {
                "type": "string",
                "description": (
                    "Wireshark display filter to apply (e.g. 'dns', 'tcp.port == 443', "
                    "'ip.src == 10.0.0.1 && http'). Used in 'filter' and 'flows' modes. "
                    "Mutually preferred over bpf_filter."
                ),
            },
            "bpf_filter": {
                "type": "string",
                "description": (
                    "BPF / tcpdump capture filter syntax (e.g. 'host 10.0.0.1 and port 80'). "
                    "Used in 'filter' mode when display_filter is not provided."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of packets or flows to return. Default: 100.",
                "default": 100,
                "minimum": 1,
                "maximum": 5000,
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Background threshold: fall back to Celery for large/slow captures ──
    async def should_run_background(self, params: dict, config: dict) -> bool:  # noqa: ARG002
        """Run in background immediately if the file is estimated to be large.

        The file size is not known here without a DB lookup, so we conservatively
        always prefer synchronous execution and let the timeout trigger the
        Celery fallback for large PCAPs.
        """
        return self.always_background

    # ── Main entry point ────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.monotonic()

        # ── Parameter extraction and validation ─────────────────────────────
        file_id = params.get("file_id", "")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_INPUT", "'file_id' is required and must be a string.")

        try:
            uuid.UUID(file_id)
        except ValueError:
            return self._failure("INVALID_INPUT", f"'file_id' is not a valid UUID: {file_id!r}")

        mode = params.get("mode", "overview")
        if mode not in {"overview", "filter", "flows", "security"}:
            return self._failure(
                "INVALID_INPUT",
                f"'mode' must be 'overview', 'filter', 'flows', or 'security'. Got: {mode!r}",
            )

        display_filter: Optional[str] = params.get("display_filter") or None
        if display_filter is not None:
            err = _validate_filter(display_filter, "display_filter")
            if err:
                return self._failure("INVALID_INPUT", err)

        bpf_filter: Optional[str] = params.get("bpf_filter") or None
        if bpf_filter is not None:
            err = _validate_filter(bpf_filter, "bpf_filter")
            if err:
                return self._failure("INVALID_INPUT", err)

        limit = params.get("limit", 100)
        if not isinstance(limit, int) or not (1 <= limit <= 5000):
            return self._failure("INVALID_INPUT", "'limit' must be an integer between 1 and 5000.")

        # ── Load PCAP file from MinIO ────────────────────────────────────────
        file_meta = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_PCAP_MAX_BYTES,
        )
        if file_meta is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' not found or you do not have access to it.",
            )

        filename = file_meta.get("filename", "")
        content_type = file_meta.get("content_type", "application/octet-stream")
        pcap_bytes: bytes = file_meta.get("data", b"")
        file_size = file_meta.get("size_bytes", len(pcap_bytes))

        if not _is_pcap_file(filename, content_type):
            return self._failure(
                "INVALID_FILE_TYPE",
                f"File '{filename}' does not appear to be a PCAP or PCAPng file. "
                f"Detected content type: {content_type}. "
                "Supported extensions: .pcap, .pcapng.",
            )

        if not pcap_bytes:
            return self._failure("EMPTY_FILE", f"File '{filename}' is empty.")

        if file_meta.get("truncated"):
            logger.warning(
                "pcap_analyzer: file %s was truncated to %d bytes (original: %d bytes)",
                file_id,
                len(pcap_bytes),
                file_size,
            )

        # ── Write to temp file (tshark requires a file path) ─────────────────
        suffix = ".pcapng" if filename.lower().endswith(".pcapng") else ".pcap"
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False, prefix="gsage_ai_pcap_"
            ) as tmp:
                tmp.write(pcap_bytes)
                tmp_path = tmp.name

            logger.info(
                "pcap_analyzer: analyzing file=%s mode=%s size=%d bytes org=%s",
                file_id,
                mode,
                len(pcap_bytes),
                agent_context.org_id,
            )

            # ── Dispatch by mode ─────────────────────────────────────────────
            if mode == "overview":
                data = await _analyze_overview(tmp_path, limit)
            elif mode == "filter":
                data = await _analyze_filter(tmp_path, display_filter, bpf_filter, limit)
            elif mode == "security":
                data = await _analyze_security(tmp_path, limit)
            else:  # flows
                data = await _analyze_flows(tmp_path, display_filter, limit)

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # ── Metadata envelope ────────────────────────────────────────────────
        elapsed = int((time.monotonic() - start) * 1000)

        data["_meta"] = {
            "file_id": file_id,
            "filename": filename,
            "file_size_bytes": file_size,
            "truncated": file_meta.get("truncated", False),
            "mode": mode,
        }

        # ── Offload large results to MinIO ────────────────────────────────────
        try:
            result_json = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            result_json = json.dumps({"error": "Result serialization failed"})

        if len(result_json.encode()) > _MAX_INLINE_BYTES:
            safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
            stored_filename = f"pcap_analysis_{safe_name}_{mode}.json"
            try:
                from src.mcp_server.tools.base import _tool_session_ctx  # noqa: PLC0415

                ctx_session = _tool_session_ctx.get()
                if ctx_session is not None:
                    file_info = await self._store_file(
                        data=result_json.encode("utf-8"),
                        filename=stored_filename,
                        content_type="application/json",
                        agent_context=agent_context,
                        session=ctx_session,
                        description=f"PCAP analysis ({mode}) for {filename}",
                    )
                else:
                    from src.shared.database import _get_session_maker  # noqa: PLC0415

                    async with _get_session_maker()() as db_session:
                        file_info = await self._store_file(
                            data=result_json.encode("utf-8"),
                            filename=stored_filename,
                            content_type="application/json",
                            agent_context=agent_context,
                            session=db_session,
                            description=f"PCAP analysis ({mode}) for {filename}",
                        )
            except Exception as exc:
                logger.error("pcap_analyzer: failed to store large result: %s", exc)
                file_info = None

            summary_data: dict = {
                "_meta": data["_meta"],
                "note": (
                    "The full analysis result exceeds the inline size limit. "
                    "Use the download link to access the complete JSON."
                ),
            }
            if file_info:
                summary_data["result_file"] = file_info
            # Include a condensed version of top-level stats when available
            for key in ("statistics", "protocol_distribution", "top_source_ips", "top_dest_ips", "alerts"):
                if key in data:
                    summary_data[key] = data[key]

            return self._partial(
                summary_data,
                code="RESULT_OFFLOADED",
                message=(
                    "Result was too large to return inline. "
                    "A summary is shown; full details are in the linked file."
                ),
                execution_time_ms=elapsed,
            )

        return self._success(data, execution_time_ms=elapsed)
