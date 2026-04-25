"""Shared URL extraction and classification utilities.

Used by both ``pdf_analyzer`` and ``eml_analyzer`` tools to avoid duplication.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# URL shortener domains
URL_SHORTENERS: frozenset[str] = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "buff.ly",
    "rebrand.ly", "shorturl.at", "is.gd", "v.gd", "cutt.ly", "tiny.cc",
})

# Suspicious TLDs commonly seen in phishing / malware distribution
SUSPICIOUS_TLDS: frozenset[str] = frozenset({
    ".xyz", ".top", ".buzz", ".click", ".link", ".loan", ".men",
    ".win", ".download", ".review", ".date", ".faith", ".trade",
    ".stream", ".gq", ".ml", ".cf", ".ga", ".tk",
})

# Ports that are normal for HTTP/FTP traffic (not suspicious)
_NORMAL_PORTS: frozenset[int] = frozenset({80, 443, 8080, 8443, 21, 22})

# Regex to extract URLs from unstructured text
URL_RE = re.compile(
    r"(?:https?|ftp|ftps)://[^\s\"'<>\]\[(){}|\\^`\x00-\x1f]{3,512}",
    re.IGNORECASE,
)

# IP-only URL pattern (no domain name)
_IP_URL_RE = re.compile(
    r"(?:https?|ftp)://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:[:/]|$)",
    re.IGNORECASE,
)

# Data URI
_DATA_URI_RE = re.compile(r"^data:", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_url(url: str) -> tuple[str, list[str]]:
    """Return ``(suspicion_level, reasons)`` for a URL.

    Suspicion levels: ``"high"``, ``"medium"``, ``"low"``

    Checks performed (in order of severity):
    - ``data:`` URI
    - IP-only host (no domain name)
    - Known URL shortener
    - Suspicious TLD
    - Excessive subdomain depth (>= 4 dots)
    - Non-standard port
    """
    reasons: list[str] = []

    if _DATA_URI_RE.match(url):
        reasons.append("data: URI (inline content, possible obfuscation)")
        return "high", reasons

    if _IP_URL_RE.match(url):
        reasons.append("IP-based URL (no domain name)")
        return "high", reasons

    try:
        from urllib.parse import urlparse  # noqa: PLC0415
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except Exception:
        host = ""
        parsed = None  # type: ignore[assignment]

    if host:
        for shortener in URL_SHORTENERS:
            if host == shortener or host.endswith("." + shortener):
                reasons.append(f"URL shortener ({shortener})")
                return "high", reasons

        for tld in SUSPICIOUS_TLDS:
            if host.endswith(tld):
                reasons.append(f"Suspicious TLD ({tld})")
                return "high", reasons

        subdomain_count = host.count(".")
        if subdomain_count >= 4:
            reasons.append(f"Excessive subdomains ({subdomain_count} dots in hostname)")
            return "medium", reasons

        if parsed is not None:
            port = parsed.port
            if port and port not in _NORMAL_PORTS:
                reasons.append(f"Non-standard port ({port})")
                return "medium", reasons

    return "low", reasons
