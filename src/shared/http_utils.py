"""gSage AI — Shared HTTP utilities.

Reusable async helpers for HTTP fetch, HTML→Markdown conversion, DOM
parsing, and Brazilian date parsing.  Used by ``http_fetch`` (core) and
``public_alerts`` (threat_intel) tools.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from trafilatura import extract

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
_SSRF_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
]

_DEFAULT_USER_AGENT = (
    "gSage-SOC/0.6 (security-orchestration; +https://gsage.ai)"
)
_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 60.0
_MAX_CONTENT_LENGTH = 20_000_000

# Brazilian date formats
_BR_DATE_PATTERNS = [
    # DD/MM/AAAA or DD/MM/AA
    (re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})"), "dmy"),
    # AAAA-MM-DD
    (re.compile(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})"), "ymd"),
    # "X de MÊS de AAAA" (Portuguese long form)
    (
        re.compile(
            r"(\d{1,2})\s+de\s+(janeiro|fevereiro|mar[çc]o|abril|maio|junho"
            r"|julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})",
            re.IGNORECASE,
        ),
        "long_br",
    ),
]

_BR_MONTHS: dict[str, int] = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}


# ── URL validation ───────────────────────────────────────────────────────────


def _validate_url(url: str) -> None:
    """Raise :class:`ValueError` if *url* is unsafe or invalid."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Unsupported URL scheme {parsed.scheme!r}. Only http/https allowed."
        )
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no resolvable hostname.")

    # Resolve hostname → IP (synchronous, but DNS is fast; we're inside
    # an async context so use loop.run_in_executor in the caller if needed).
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        # Not an IP literal — DNS resolution happens in httpx.  We trust
        # that httpx raises on SSRF later, but also do a pre-check for
        # IP-literal URLs above.
        return

    for net in _SSRF_BLOCKED_NETWORKS:
        if addr in net:
            raise ValueError(
                f"URL hostname {hostname!r} resolves to a private/internal "
                f"IP address ({addr}).  Blocked to prevent SSRF."
            )


# ── HTTP fetch ────────────────────────────────────────────────────────────────


async def fetch_url(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[dict] = None,
    body: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
    max_content_length: int = _MAX_CONTENT_LENGTH,
) -> dict:
    """Fetch a URL and return a dict with status, headers, and body.

    Parameters
    ----------
    url:
        Full http/https URL.
    method:
        HTTP method (GET or POST).
    headers:
        Extra request headers.  ``User-Agent`` defaults to gSage-SOC.
    body:
        Request body for POST.
    timeout:
        Total timeout in seconds (clamped to 60).
    follow_redirects:
        Whether to follow 3xx redirects.
    max_content_length:
        Hard cap on response body bytes.

    Returns
    -------
    dict
        ``{"status_code": int, "content_type": str, "content_length": int,
        "body": bytes, "title": str | None, "final_url": str}``

    Raises
    ------
    ValueError
        Invalid or unsafe URL.
    httpx.HTTPError
        Transport-level failure (timeout, DNS, connection refused, …).
    """
    _validate_url(url)
    timeout = min(timeout, _MAX_TIMEOUT)
    merged_headers = {"User-Agent": _DEFAULT_USER_AGENT}
    if headers:
        merged_headers.update(headers)

    transport = httpx.AsyncHTTPTransport(retries=2)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=merged_headers,
        transport=transport,
    ) as client:
        if method.upper() == "POST":
            resp = await client.post(url, content=body or "")
        else:
            resp = await client.get(url)

        # Read up to max_content_length
        raw = b""
        async for chunk in resp.aiter_bytes():
            raw += chunk
            if len(raw) > max_content_length:
                raw = raw[:max_content_length]
                break

    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_type = content_type.split(";")[0].strip()

    # Attempt to extract a title from HTML
    title: Optional[str] = None
    ct_lower = content_type.lower()
    if ct_lower.startswith("text/html"):
        try:
            decoded = raw.decode("utf-8", errors="replace")[:200_000]
            title = _extract_title(decoded)
        except Exception:
            pass

    return {
        "status_code": resp.status_code,
        "content_type": content_type,
        "content_length": len(raw),
        "body": raw,
        "title": title,
        "final_url": str(resp.url),
    }


def _extract_title(html: str) -> Optional[str]:
    """Extract <title> or first <h1> from HTML snippet."""
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("title")
    if tag and tag.get_text(strip=True):
        return tag.get_text(strip=True)[:500]
    tag = soup.find("h1")
    if tag and tag.get_text(strip=True):
        return tag.get_text(strip=True)[:500]
    return None


# ── HTML → Markdown ──────────────────────────────────────────────────────────


def html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown using ``trafilatura``.

    Falls back to the raw HTML (truncated) if conversion fails.
    """
    try:
        md = extract(html, output_format="markdown", with_metadata=True)
        if md and md.strip():
            return md.strip()
    except Exception:
        log.warning("trafilatura extraction failed, falling back to raw HTML")
    # Fallback: return raw text stripped of tags via BeautifulSoup
    try:
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text("\n", strip=True)[:50_000]
    except Exception:
        return html[:10_000]


# ── DOM parsing ──────────────────────────────────────────────────────────────


def parse_html_dom(html: str) -> BeautifulSoup:
    """Parse HTML into a BeautifulSoup object with ``lxml`` backend."""
    return BeautifulSoup(html, "lxml")


# ── Brazilian date parsing ───────────────────────────────────────────────────


def parse_date_br(text: str) -> Optional[date]:
    """Parse a date from Brazilian-format text.

    Supported formats:

    - ``DD/MM/AAAA`` / ``DD/MM/AA``
    - ``AAAA-MM-DD`` (ISO)
    - ``X de mês de AAAA`` (Portuguese long form, e.g. "15 de janeiro de 2026")

    Returns ``None`` if no date could be parsed.
    """
    text = text.strip()
    for pattern, fmt in _BR_DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if fmt == "dmy":
                day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if year < 100:
                    year += 2000
                return date(year, month, day)
            elif fmt == "ymd":
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return date(year, month, day)
            elif fmt == "long_br":
                day = int(m.group(1))
                month_name = m.group(2).lower()
                month = _BR_MONTHS.get(month_name)
                year = int(m.group(3))
                if month:
                    return date(year, month, day)
        except (ValueError, OverflowError):
            continue
    return None


# ── URL helpers ───────────────────────────────────────────────────────────────


def url_slug(url: str) -> str:
    """Generate a short, filesystem-safe slug from a URL for filenames."""
    parsed = urlparse(url)
    stem = (parsed.hostname or "unknown") + (parsed.path or "")
    stem = re.sub(r"[^a-zA-Z0-9._-]", "_", stem)[:60]
    return stem.strip("_") or "fetch"


def url_hash(url: str) -> str:
    """SHA-256 hex digest of the canonical URL (for cache keys)."""
    return hashlib.sha256(url.encode()).hexdigest()[:32]
