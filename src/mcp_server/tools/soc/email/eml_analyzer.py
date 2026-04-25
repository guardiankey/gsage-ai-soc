"""gSage AI — EML Security Analyzer tool.

Performs a security-focused static analysis of an email file (.eml) uploaded
as a chat attachment.  A single call returns a comprehensive report covering:

  - Cryptographic hashes (MD5, SHA1, SHA256)
  - Envelope metadata (From, To, Subject, Date, Message-ID, X-Mailer, …)
  - Authentication assessment (SPF, DKIM, DMARC parsed from headers)
  - Received-chain analysis (routing hops, IP extraction, anomaly detection)
  - Header anomaly detection (From/Reply-To mismatch, display-name spoofing, …)
  - Body content analysis (urgency keywords, hidden HTML, form elements, …)
  - URL extraction with suspicion classification (phishing / redirect chains)
  - Attachment inventory (filenames, sizes, types, executable-extension flags)
  - Aggregated risk summary (high / medium / low / clean)
"""

from __future__ import annotations

import email
import email.header
import email.message
import email.policy
import email.utils
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import uuid
from email.parser import BytesParser
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.threat_intel._url_utils import (
    URL_RE as _URL_RE,
    classify_url as _classify_url,
)
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max bytes to load from MinIO (50 MB — EML files are typically much smaller)
_EML_MAX_BYTES = 50 * 1024 * 1024

# Max result size before offloading to MinIO (50 KB)
_MAX_INLINE_BYTES = 50 * 1024

# Max body text to analyse (full extraction; trimmed to max_body_chars on output)
_BODY_ANALYSIS_MAX = 200_000

# Max URLs to return
_MAX_URLS = 150

# Max attachments to enumerate
_MAX_ATTACHMENTS = 50

# Max chars per received header to store (they can be very long)
_RECEIVED_MAX_CHARS = 300

# Accepted EML MIME types
_EML_CONTENT_TYPES = {
    "message/rfc822",
    "message/rfc2822",
    "text/plain",           # sometimes .eml is served as text/plain
    "application/octet-stream",
}

# Executable attachment extensions → immediate HIGH risk
_EXECUTABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".scr", ".com", ".hta", ".msi",
    ".pif", ".reg", ".jar", ".swf", ".lnk", ".cpl",
})

# Archives that can contain executables
_ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({
    ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz", ".iso", ".img",
})

# Patterns indicating urgency / social engineering in email bodies
_URGENCY_PHRASES: list[str] = [
    "verify your account",
    "confirm your identity",
    "update your information",
    "act now",
    "act immediately",
    "urgent action required",
    "your account has been suspended",
    "your account will be closed",
    "click here immediately",
    "limited time",
    "account locked",
    "unusual activity",
    "security alert",
    "you have been selected",
    "congratulations you won",
    "claim your prize",
    "wire transfer",
    "gift card",
    "confirm your payment",
    "invoice attached",
    "your password has expired",
    "log in to continue",
]

# Regex for IP addresses in Received headers
_IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

# Regex for private IP ranges (RFC 1918, loopback, APIPA)
_PRIVATE_IP_RE = re.compile(
    r"^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|127\.|169\.254\.)"
)

# Regex for suspicious paths in URLs (login/verify/etc.)
_SUSPICIOUS_PATH_RE = re.compile(
    r"(?:login|signin|sign-in|verify|verification|account|secure|update|confirm|"
    r"validate|banking|webscr|ebayisapi|paypal)",
    re.IGNORECASE,
)

# HTML form / script tags in body
_HTML_FORM_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
_HTML_SCRIPT_RE = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_HTML_IFRAME_RE = re.compile(
    r"<iframe\b[^>]*(?:style\s*=\s*['\"][^'\"]*(?:display\s*:\s*none|visibility\s*:\s*hidden)[^'\"]*['\"]|[^>]*)>",
    re.IGNORECASE,
)
# href extraction from HTML
_HTML_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']{4,512})["\']', re.IGNORECASE)
# anchor text vs href (display vs actual)
_HTML_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href\s*=\s*["\']([^"\']{4,512})["\'][^>]*>\s*([^<]{4,200})\s*</a>',
    re.IGNORECASE,
)

# Known URL shortener domains — links using these services obscure the real destination
_KNOWN_SHORTENERS: frozenset[str] = frozenset({
    "bit.ly", "bitly.com",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "ow.ly",
    "buff.ly",
    "is.gd",
    "tiny.cc",
    "lnkd.in",
    "dlvr.it",
    "short.link",
    "rb.gy",
    "cutt.ly",
    "shorturl.at",
    "shorturl.com",
    "clck.ru",
    "su.pr",
    "snip.ly",
    "rebrand.ly",
    "mcaf.ee",
    "qr.ae",
    "tr.im",
    "x.co",
    "yourls.org",
    "cli.gs",
    "ff.im",
    "j.mp",
    "po.st",
    "soo.gd",
    "u.to",
    "v.gd",
    "vzturl.com",
    "wp.me",
    "yep.it",
})

# Homoglyph / confusable unicode detection in domain names
_CONFUSABLE_CHARS = re.compile(
    r"[\u0430\u0435\u043e\u0440\u0441\u0445\u0456\u0443"  # Cyrillic: а е о р с х і у
    r"\u03b1\u03b5\u03bf\u03c1\u03c5"                      # Greek: α ε ο ρ υ
    r"\u0131\u026a\u0269\u04cf]",                          # dotless-i and others
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_hashes(data: bytes) -> dict[str, str]:
    """Return MD5, SHA1, and SHA256 hex digests for *data*."""
    return {
        "md5": hashlib.md5(data).hexdigest(),  # noqa: S324
        "sha1": hashlib.sha1(data).hexdigest(),  # noqa: S324
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _decode_header_value(value: Optional[str]) -> str:
    """Decode a potentially RFC 2047-encoded header value to a plain string."""
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
        decoded = []
        for part_bytes, charset in parts:
            if isinstance(part_bytes, bytes):
                decoded.append(part_bytes.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(part_bytes))
        return " ".join(decoded).strip()
    except Exception:
        return str(value)


def _parse_address(value: str) -> dict:
    """Parse a single address header into {display_name, email, domain}."""
    try:
        name, addr = email.utils.parseaddr(value)
        name = _decode_header_value(name)
        addr = addr.strip().lower()
        domain = addr.split("@", 1)[1] if "@" in addr else ""
        return {"display_name": name, "email": addr, "domain": domain}
    except Exception:
        return {"display_name": "", "email": value, "domain": ""}


def _parse_address_list(value: str) -> list[dict]:
    """Parse a comma-separated address list header."""
    try:
        pairs = email.utils.getaddresses([value])
        result = []
        for name, addr in pairs:
            name = _decode_header_value(name)
            addr = addr.strip().lower()
            domain = addr.split("@", 1)[1] if "@" in addr else ""
            result.append({"display_name": name, "email": addr, "domain": domain})
        return result
    except Exception:
        return []


def _has_homoglyphs(text: str) -> bool:
    """Return True if *text* contains suspected confusable / homoglyph characters."""
    return bool(_CONFUSABLE_RE.search(text))


# Build the confusable regex once
_CONFUSABLE_RE = _CONFUSABLE_CHARS

# Also detect punycode domains
_PUNYCODE_RE = re.compile(r"\bxn--[a-zA-Z0-9\-]+\b")


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------


def _extract_envelope(msg: email.message.Message) -> dict:
    """Extract all envelope / addressing headers."""
    envelope: dict = {}

    for field in ("Subject", "Date", "Message-ID", "In-Reply-To", "References"):
        raw = msg.get(field)
        if raw:
            envelope[field.lower().replace("-", "_")] = _decode_header_value(raw)

    for field in ("From", "Reply-To", "Return-Path", "Sender"):
        raw = msg.get(field)
        if raw:
            envelope[field.lower().replace("-", "_")] = _parse_address(raw)

    for field in ("To", "CC", "BCC"):
        raw = msg.get(field)
        if raw:
            envelope[field.lower()] = _parse_address_list(raw)

    # X-headers
    for field in ("X-Mailer", "X-Originating-IP", "X-Spam-Status",
                  "X-Spam-Score", "List-Unsubscribe", "Precedence"):
        raw = msg.get(field)
        if raw:
            envelope[field.lower().replace("-", "_")] = _decode_header_value(raw)

    # Detect defects (RFC violations)
    defects = getattr(msg, "defects", [])
    if defects:
        envelope["parsing_defects"] = [str(d) for d in defects]

    return envelope


def _assess_authentication(msg: email.message.Message) -> dict:
    """Parse email authentication headers (SPF, DKIM, DMARC).

    Does NOT perform live DNS lookups — only reads existing headers.
    """
    auth: dict = {
        "spf": None,
        "dkim": None,
        "dmarc": None,
        "arc": None,
        "raw_auth_results": [],
    }

    # Authentication-Results (may be present multiple times)
    for ar in msg.get_all("Authentication-Results") or []:
        auth["raw_auth_results"].append(_decode_header_value(ar))
        ar_lower = ar.lower()
        if "spf=pass" in ar_lower:
            auth["spf"] = "pass"
        elif "spf=fail" in ar_lower:
            auth["spf"] = "fail"
        elif "spf=softfail" in ar_lower:
            auth["spf"] = "softfail"
        elif "spf=neutral" in ar_lower or "spf=none" in ar_lower:
            auth["spf"] = auth["spf"] or "none"

        if "dkim=pass" in ar_lower:
            auth["dkim"] = "pass"
        elif "dkim=fail" in ar_lower:
            auth["dkim"] = "fail"
        elif "dkim=none" in ar_lower:
            auth["dkim"] = auth["dkim"] or "none"

        if "dmarc=pass" in ar_lower:
            auth["dmarc"] = "pass"
        elif "dmarc=fail" in ar_lower:
            auth["dmarc"] = "fail"
        elif "dmarc=none" in ar_lower:
            auth["dmarc"] = auth["dmarc"] or "none"

    # Received-SPF header (older format)
    spf_hdr = msg.get("Received-SPF")
    if spf_hdr and auth["spf"] is None:
        spf_lower = spf_hdr.lower()
        if "pass" in spf_lower:
            auth["spf"] = "pass"
        elif "fail" in spf_lower:
            auth["spf"] = "fail"

    # DKIM-Signature header (just presence / domain info)
    dkim_sig = msg.get("DKIM-Signature")
    if dkim_sig:
        auth["dkim_signature_present"] = True
        domain_m = re.search(r"\bd=([^\s;]+)", dkim_sig)
        sel_m = re.search(r"\bs=([^\s;]+)", dkim_sig)
        if domain_m:
            auth["dkim_signing_domain"] = domain_m.group(1).strip()
        if sel_m:
            auth["dkim_selector"] = sel_m.group(1).strip()

    # ARC (Authenticated Received Chain)
    arc_hdr = msg.get("ARC-Authentication-Results")
    if arc_hdr:
        auth["arc"] = _decode_header_value(arc_hdr)[:256]

    # Clean up empty raw_auth_results
    if not auth["raw_auth_results"]:
        del auth["raw_auth_results"]

    return auth


def _analyse_received_chain(msg: email.message.Message) -> dict:
    """Parse all Received headers to reconstruct the routing chain."""
    received_raw = msg.get_all("Received") or []
    hops: list[dict] = []

    for raw in reversed(received_raw):  # oldest hop first
        hop: dict = {"raw": raw.strip()[:_RECEIVED_MAX_CHARS]}

        # Extract IPs
        ips = _IP_RE.findall(raw)
        if ips:
            hop["ips"] = list(dict.fromkeys(ips))  # deduplicate, preserve order
            hop["private_ips"] = [ip for ip in hop["ips"] if _PRIVATE_IP_RE.match(ip)]

        # Extract hostname (from clause)
        from_m = re.search(r"\bfrom\s+(\S+)", raw, re.IGNORECASE)
        if from_m:
            hop["from_host"] = from_m.group(1)

        by_m = re.search(r"\bby\s+(\S+)", raw, re.IGNORECASE)
        if by_m:
            hop["by_host"] = by_m.group(1)

        # Timestamp
        ts_m = re.search(
            r";\s*(.{20,50}(?:\+\d{4}|-\d{4}|GMT|UTC|PST|EST|CST|MST))",
            raw, re.IGNORECASE,
        )
        if ts_m:
            hop["timestamp_raw"] = ts_m.group(1).strip()

        hops.append(hop)

    anomalies: list[str] = []

    # Detect private IPs appearing in multi-hop chains (could indicate spoofing)
    if len(hops) > 1:
        for i, hop in enumerate(hops[:-1]):  # skip last (usually local MDA)
            for ip in hop.get("private_ips", []):
                anomalies.append(
                    f"Private IP {ip} found in external hop {i + 1} (possible spoofing indicator)"
                )

    return {
        "hop_count": len(hops),
        "hops": hops,
        "anomalies": anomalies,
    }


def _detect_header_anomalies(
    msg: email.message.Message,
    envelope: dict,
) -> list[dict]:
    """Detect header-level phishing and spoofing indicators."""
    anomalies: list[dict] = []

    from_obj = envelope.get("from_") or envelope.get("from") or {}
    reply_to_obj = envelope.get("reply_to") or {}
    return_path_obj = envelope.get("return_path") or {}

    from_email = from_obj.get("email", "")
    from_domain = from_obj.get("domain", "")
    from_display = from_obj.get("display_name", "")
    reply_to_email = reply_to_obj.get("email", "")
    return_path_email = return_path_obj.get("email", "")

    # 1. From ≠ Reply-To (classic "reply hijack")
    if reply_to_email and from_email and reply_to_email != from_email:
        domain_rt = reply_to_email.split("@", 1)[1] if "@" in reply_to_email else ""
        domain_from = from_email.split("@", 1)[1] if "@" in from_email else ""
        anomalies.append({
            "type": "reply_to_mismatch",
            "severity": "medium",
            "description": (
                f"Reply-To ({reply_to_email}) differs from From ({from_email}). "
                "Replies will go to a different address than the sender."
            ),
        })
        if domain_rt != domain_from:
            # Escalate if the domain also differs
            anomalies[-1]["severity"] = "high"
            anomalies[-1]["description"] += " Reply-To domain is also different."

    # 2. Return-Path ≠ From (envelope sender mismatch)
    if return_path_email and from_email and return_path_email != from_email:
        anomalies.append({
            "type": "return_path_mismatch",
            "severity": "medium",
            "description": (
                f"Return-Path ({return_path_email}) differs from From ({from_email}). "
                "Bounce messages will not go to the displayed sender."
            ),
        })

    # 3. Display-name spoofing (brand name in display, random domain)
    if from_display and from_domain:
        brand_keywords = [
            "paypal", "amazon", "ebay", "google", "microsoft", "apple",
            "bank", "chase", "wellsfargo", "citibank", "netflix", "facebook",
            "instagram", "twitter", "linkedin", "dropbox", "icloud",
        ]
        display_lower = from_display.lower()
        for brand in brand_keywords:
            if brand in display_lower and brand not in from_domain:
                anomalies.append({
                    "type": "display_name_spoofing",
                    "severity": "high",
                    "description": (
                        f"Display name '{from_display}' suggests brand '{brand}' "
                        f"but the sender domain is '{from_domain}'."
                    ),
                })
                break

    # 4. Homoglyphs / Punycode in From domain
    if from_domain:
        if _CONFUSABLE_RE.search(from_domain):
            anomalies.append({
                "type": "homoglyph_domain",
                "severity": "high",
                "description": (
                    f"From domain '{from_domain}' contains confusable/homoglyph unicode "
                    "characters that may visually resemble a legitimate domain."
                ),
            })
        if _PUNYCODE_RE.search(from_domain):
            anomalies.append({
                "type": "punycode_domain",
                "severity": "medium",
                "description": (
                    f"From domain '{from_domain}' contains punycode encoding (xn--), "
                    "which can be used to impersonate legitimate domains."
                ),
            })

    # 5. Missing Message-ID
    if not envelope.get("message_id"):
        anomalies.append({
            "type": "missing_message_id",
            "severity": "low",
            "description": "No Message-ID header found. RFC 5322 requires it; absence may indicate bulk or forged mail.",
        })

    # 6. Suspicious X-Mailer
    x_mailer = envelope.get("x_mailer", "")
    if x_mailer:
        mailer_lower = x_mailer.lower()
        suspicious_mailers = ["phpmailer", "sendblaster", "bulkmailer", "massmailer",
                               "group mail", "emailer pro"]
        for sm in suspicious_mailers:
            if sm in mailer_lower:
                anomalies.append({
                    "type": "suspicious_mailer",
                    "severity": "low",
                    "description": f"X-Mailer '{x_mailer}' is associated with bulk/spam mailers.",
                })
                break

    return anomalies


def _analyse_body(msg: email.message.Message, max_chars: int) -> dict:
    """Extract and analyse the email body content."""
    text_plain = ""
    text_html = ""
    html_anomalies: list[dict] = []

    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True)
            if not payload or not isinstance(payload, bytes):
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")

            if ct == "text/plain" and not text_plain:
                text_plain = decoded[:_BODY_ANALYSIS_MAX]
            elif ct == "text/html" and not text_html:
                text_html = decoded[:_BODY_ANALYSIS_MAX]
        except Exception:
            continue

    # HTML analysis
    if text_html:
        form_matches = _HTML_FORM_RE.findall(text_html)
        if form_matches:
            html_anomalies.append({
                "type": "html_form",
                "severity": "high",
                "description": f"HTML body contains {len(form_matches)} form element(s). May be used to collect credentials.",
                "count": len(form_matches),
            })

        script_matches = _HTML_SCRIPT_RE.findall(text_html)
        if script_matches:
            html_anomalies.append({
                "type": "html_script",
                "severity": "high",
                "description": f"HTML body contains {len(script_matches)} <script> element(s).",
                "count": len(script_matches),
            })

        if _HTML_IFRAME_RE.search(text_html):
            html_anomalies.append({
                "type": "hidden_iframe",
                "severity": "high",
                "description": "HTML body contains a hidden iframe (display:none or visibility:hidden).",
            })

    # Urgency keyword detection
    body_for_keywords = (text_plain + " " + text_html).lower()
    matched_phrases = [
        phrase for phrase in _URGENCY_PHRASES
        if phrase in body_for_keywords
    ]

    # Body preview (prefer plain text; fall back to stripped HTML)
    if text_plain:
        preview_source = text_plain
    elif text_html:
        # Strip HTML tags for a text preview
        preview_source = re.sub(r"<[^>]+>", " ", text_html)
        preview_source = re.sub(r"\s+", " ", preview_source).strip()
    else:
        preview_source = ""

    return {
        "has_plain_text": bool(text_plain),
        "has_html": bool(text_html),
        "html_anomalies": html_anomalies,
        "urgency_phrases_found": matched_phrases,
        "body_preview": preview_source[:max_chars] if max_chars > 0 else "",
        "_raw_html": text_html,   # kept for URL extraction, removed from final output
        "_raw_plain": text_plain, # kept for URL extraction, removed from final output
    }


def _extract_urls(body_data: dict, envelope: dict) -> list[dict]:
    """Extract all URLs from email body and headers."""
    seen: set[str] = set()
    urls: list[dict] = []

    from_domain = (envelope.get("from_") or {}).get("domain", "")

    def _add(url: str, source: str, display_text: Optional[str] = None) -> None:
        url = url.strip().rstrip(">),;\"'")
        if not url or url in seen or len(urls) >= _MAX_URLS:
            return
        seen.add(url)
        level, reasons = _classify_url(url)

        # Detect URL shortener services — real destination is hidden
        is_shortened = False
        try:
            from urllib.parse import urlparse as _urlparse  # noqa: PLC0415
            _parsed_host = (_urlparse(url).hostname or "").lower().lstrip("www.")
            if _parsed_host in _KNOWN_SHORTENERS:
                is_shortened = True
                if level == "low":
                    level = "medium"
                reasons.append(
                    f"URL uses a known shortener service ({_parsed_host}) — real destination is hidden"
                )
        except Exception:
            pass

        # Email-specific URL check: suspicious path keywords
        if level == "low" and _SUSPICIOUS_PATH_RE.search(url):
            level = "medium"
            reasons.append("URL path contains security-sensitive keyword (login/verify/account/…)")

        # Display text ≠ actual URL (classic phishing indicator)
        if display_text:
            display_clean = display_text.strip().lower()
            # Check if display text looks like a URL but differs from actual
            if (display_clean.startswith("http") or "." in display_clean) and display_clean != url.lower():
                # Simple domain comparison — does the display domain appear in the real URL?
                try:
                    from urllib.parse import urlparse  # noqa: PLC0415
                    parsed = urlparse(url)
                    real_host = (parsed.hostname or "").lower()
                    # Extract what looks like a domain from display text
                    display_domain_m = re.search(r"([a-z0-9\-]+\.[a-z]{2,})", display_clean)
                    display_domain = display_domain_m.group(1) if display_domain_m else ""
                    if display_domain and real_host and display_domain not in real_host:
                        level = "high"
                        reasons.append(
                            f"Display text '{display_text[:60]}' suggests a different domain than the actual URL"
                        )
                except Exception:
                    pass

        entry: dict = {
            "url": url,
            "source": source,
            "suspicion": level,
            "reasons": reasons,
            "is_shortened": is_shortened,
        }
        if display_text and display_text.strip():
            entry["display_text"] = display_text.strip()[:120]
        urls.append(entry)

    # HTML hrefs from anchor tags (with display text comparison)
    html_body = body_data.get("_raw_html", "")
    if html_body:
        for m in _HTML_ANCHOR_RE.finditer(html_body):
            href, text = m.group(1), m.group(2)
            _add(href, "html_anchor", text)
        # Also pick up hrefs that didn't have visible text
        for m in _HTML_HREF_RE.finditer(html_body):
            _add(m.group(1), "html_href")

    # URLs from plain text body
    plain_body = body_data.get("_raw_plain", "")
    for m in _URL_RE.finditer(plain_body):
        _add(m.group(0), "text_body")

    # Sort: high suspicion first
    _order = {"high": 0, "medium": 1, "low": 2}
    urls.sort(key=lambda u: _order.get(u["suspicion"], 3))
    return urls


def _inventory_attachments(msg: email.message.Message) -> list[dict]:
    """Walk all MIME parts and inventory non-body attachments."""
    attachments: list[dict] = []

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        # Skip body text parts
        disposition = part.get("Content-Disposition", "")
        is_attachment = "attachment" in disposition.lower()
        ct = part.get_content_type()
        is_body = ct in ("text/plain", "text/html") and not is_attachment
        if is_body and not is_attachment:
            continue

        # Some inline parts also count (e.g. "inline" with a filename)
        filename_raw = part.get_filename()
        if not filename_raw and not is_attachment:
            continue

        filename = _decode_header_value(filename_raw) if filename_raw else "(unnamed)"
        ext = os.path.splitext(filename.lower())[1]

        entry: dict = {
            "filename": filename,
            "content_type": ct,
            "extension": ext,
        }

        # Size
        try:
            payload = part.get_payload(decode=True)
            if payload and isinstance(payload, bytes):
                entry["size_bytes"] = len(payload)
                entry["sha256"] = hashlib.sha256(payload).hexdigest()
            else:
                entry["size_bytes"] = 0
        except Exception:
            entry["size_bytes"] = -1

        # Double extension check
        basename = os.path.splitext(filename)[0]
        inner_ext = os.path.splitext(basename.lower())[1]
        if inner_ext and ext:
            entry["double_extension"] = True
            entry["double_extension_detail"] = f"'{filename}' has two extensions"

        # Flag executable or archive
        entry["suspicious_executable"] = ext in _EXECUTABLE_EXTENSIONS
        entry["is_archive"] = ext in _ARCHIVE_EXTENSIONS
        if entry["suspicious_executable"]:
            entry["reason"] = f"Executable file extension ({ext})"

        if len(attachments) < _MAX_ATTACHMENTS:
            attachments.append(entry)

    return attachments


def _build_risk_summary(
    envelope: dict,
    auth: dict,
    received: dict,
    header_anomalies: list[dict],
    body_data: dict,
    urls: list[dict],
    attachments: list[dict],
) -> dict:
    """Aggregate all signals into a risk level and structured findings."""
    findings: list[dict] = []

    # ── HIGH ────────────────────────────────────────────────────────────────

    # Executable attachments
    for att in attachments:
        if att.get("suspicious_executable"):
            findings.append({
                "severity": "high",
                "category": "attachment",
                "description": f"Executable attachment: {att['filename']} ({att.get('reason', '')})",
            })
        if att.get("double_extension"):
            findings.append({
                "severity": "high",
                "category": "attachment",
                "description": att.get("double_extension_detail", "Double file extension detected"),
            })

    # HTML form / script / hidden iframe
    for ha in body_data.get("html_anomalies", []):
        if ha["severity"] == "high":
            findings.append({
                "severity": "high",
                "category": "html_content",
                "description": ha["description"],
            })

    # Header anomalies rated HIGH
    for anomaly in header_anomalies:
        if anomaly["severity"] == "high":
            findings.append({
                "severity": "high",
                "category": "header_anomaly",
                "description": anomaly["description"],
            })

    # High-suspicion URLs
    high_urls = [u for u in urls if u.get("suspicion") == "high"]
    for u in high_urls[:10]:
        reason_str = "; ".join(u.get("reasons", [])) or "unknown"
        findings.append({
            "severity": "high",
            "category": "url",
            "description": f"Suspicious URL ({reason_str}): {u['url'][:120]}",
        })

    # ── MEDIUM ──────────────────────────────────────────────────────────────

    # Shortened URLs (destination unknown)
    shortened_urls = [u for u in urls if u.get("is_shortened")]
    if shortened_urls:
        url_list = ", ".join(u["url"][:60] for u in shortened_urls[:5])
        more = f" (+{len(shortened_urls) - 5} more)" if len(shortened_urls) > 5 else ""
        findings.append({
            "severity": "medium",
            "category": "url",
            "description": (
                f"{len(shortened_urls)} shortened URL(s) found — real destination cannot be verified "
                f"without following the redirect: {url_list}{more}"
            ),
        })

    # Authentication failures
    auth_issues: list[str] = []
    if auth.get("spf") in ("fail", "softfail"):
        auth_issues.append(f"SPF {auth['spf']}")
    if auth.get("dkim") == "fail":
        auth_issues.append("DKIM fail")
    if auth.get("dmarc") == "fail":
        auth_issues.append("DMARC fail")
    if auth_issues:
        findings.append({
            "severity": "medium",
            "category": "authentication",
            "description": f"Email authentication failures: {', '.join(auth_issues)}.",
        })

    # No authentication at all
    no_spf = auth.get("spf") is None
    no_dkim = auth.get("dkim") is None
    no_dmarc = auth.get("dmarc") is None
    if no_spf and no_dkim and no_dmarc:
        findings.append({
            "severity": "medium",
            "category": "authentication",
            "description": "No SPF, DKIM, or DMARC authentication results found in headers.",
        })

    # Header anomalies rated MEDIUM
    for anomaly in header_anomalies:
        if anomaly["severity"] == "medium":
            findings.append({
                "severity": "medium",
                "category": "header_anomaly",
                "description": anomaly["description"],
            })

    # Urgency keywords
    phrases = body_data.get("urgency_phrases_found", [])
    if phrases:
        findings.append({
            "severity": "medium",
            "category": "social_engineering",
            "description": f"Urgency / social engineering phrases detected: {', '.join(repr(p) for p in phrases[:5])}.",
        })

    # Medium-suspicion URLs
    medium_urls = [u for u in urls if u.get("suspicion") == "medium"]
    if medium_urls:
        findings.append({
            "severity": "medium",
            "category": "url",
            "description": f"{len(medium_urls)} URL(s) with medium suspicion level.",
        })

    # Received chain anomalies
    for ra in received.get("anomalies", []):
        findings.append({
            "severity": "medium",
            "category": "received_chain",
            "description": ra,
        })

    # Archive attachments (may hide executables)
    archive_atts = [a for a in attachments if a.get("is_archive")]
    if archive_atts:
        findings.append({
            "severity": "medium",
            "category": "attachment",
            "description": f"{len(archive_atts)} archive attachment(s) — may contain hidden executables.",
        })

    # ── LOW ─────────────────────────────────────────────────────────────────

    # Header anomalies rated LOW
    for anomaly in header_anomalies:
        if anomaly["severity"] == "low":
            findings.append({
                "severity": "low",
                "category": "header_anomaly",
                "description": anomaly["description"],
            })

    # HTML anomalies rated low/medium
    for ha in body_data.get("html_anomalies", []):
        if ha["severity"] != "high":
            findings.append({
                "severity": ha["severity"],
                "category": "html_content",
                "description": ha["description"],
            })

    low_urls = [u for u in urls if u.get("suspicion") == "low"]
    if low_urls:
        findings.append({
            "severity": "low",
            "category": "url",
            "description": f"{len(low_urls)} benign-looking URL(s) found.",
        })

    # Determine overall risk level
    severities = {f["severity"] for f in findings}
    if "high" in severities:
        risk = "high"
    elif "medium" in severities:
        risk = "medium"
    elif "low" in severities:
        risk = "low"
    else:
        risk = "clean"

    return {
        "risk_level": risk,
        "finding_count": len(findings),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Async bridge for CPU-bound synchronous functions
# ---------------------------------------------------------------------------

import asyncio
import functools


async def _run_sync(fn: Any, *args: Any) -> Any:
    """Run a synchronous function in the default thread pool executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args))


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class EmlAnalyzerTool(BaseTool):
    """
    EML Security Analyzer — static security analysis of .eml email files.

    Performs a comprehensive one-shot analysis of an email file uploaded as
    a chat attachment.  Designed to detect phishing, spoofing, malicious
    attachments, and social engineering.

    The report covers:

        hashes          MD5, SHA1, SHA256 of the raw .eml bytes.

        envelope        From, To, CC, BCC, Reply-To, Return-Path, Subject,
                        Date, Message-ID, X-Mailer, X-Originating-IP, …

        authentication  SPF, DKIM, DMARC verdicts parsed from existing headers
                        (Authentication-Results, Received-SPF, DKIM-Signature).
                        NOTE: No live DNS lookups — header values only.

        received_chain  All Received headers in chronological order with
                        extracted IPs, hostnames, and routing anomalies.

        header_anomalies
                        From/Reply-To mismatch, Return-Path divergence,
                        display-name brand spoofing, homoglyphs/punycode
                        in From domain, missing Message-ID, suspicious mailer.

        body_analysis   Plain text / HTML body preview.  HTML is checked
                        for forms, scripts, hidden iframes, and urgency
                        keywords ("verify your account", "act now", …).

        urls            All URLs extracted from the body and HTML hrefs,
                        classified by suspicion level.  Email-specific checks
                        include display-text-vs-href mismatch and
                        security-sensitive path keywords.

        attachments     Filename, content type, size, SHA256, and flags for
                        executable extensions, double extensions, and archives.

        risk_summary    Aggregated risk level (high / medium / low / clean)
                        with a prioritised list of specific findings.

    Required parameter:
        file_id (str): UUID of the attached .eml file.

    Optional parameters:
        max_body_chars (int): Characters of body text to include in the
                              preview.  Default: 10000.  Range: 0–50000.

    Permission: ``agents:run``
    """

    name: ClassVar[str] = "eml_analyzer"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Analyze .eml email files for phishing indicators, spoofing, malicious attachments, and suspicious URLs"
    category: ClassVar[str] = "email"
    core_tool: ClassVar[bool] = True
    permissions: ClassVar[list[str]] = ["agents:run"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 60
    background_threshold_seconds: ClassVar[Optional[int]] = 30
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
                    "UUID of the attached .eml file to analyze. "
                    "Upload the file as a chat attachment first, then provide its UUID here."
                ),
            },
            "max_body_chars": {
                "type": "integer",
                "description": (
                    "Maximum number of characters of body text to include in the report "
                    "(useful for reading phishing email content). "
                    "Default: 10000. Set to 0 to skip body preview."
                ),
                "default": 10000,
                "minimum": 0,
                "maximum": 50000,
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
        start = time.monotonic()

        # ── Parameter validation ────────────────────────────────────────────
        file_id = params.get("file_id", "")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_INPUT", "'file_id' is required.")

        try:
            uuid.UUID(file_id)
        except ValueError:
            return self._failure(
                "INVALID_INPUT",
                f"'file_id' is not a valid UUID: {file_id!r}",
            )

        max_body_chars = params.get("max_body_chars", 10000)
        if not isinstance(max_body_chars, int) or not (0 <= max_body_chars <= 50000):
            return self._failure(
                "INVALID_INPUT",
                "'max_body_chars' must be an integer between 0 and 50000.",
            )

        # ── Load file from MinIO ────────────────────────────────────────────
        file_meta = await self._load_file(
            file_id=file_id,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id),
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
            max_bytes=_EML_MAX_BYTES,
        )
        if file_meta is None:
            return self._failure(
                "FILE_NOT_FOUND",
                f"File '{file_id}' was not found or you do not have access to it.",
            )

        filename: str = file_meta.get("filename", "")
        content_type: str = file_meta.get("content_type", "application/octet-stream")
        eml_bytes: bytes = file_meta.get("data", b"")
        file_size: int = file_meta.get("size_bytes", len(eml_bytes))

        # Validate file type
        suffix = os.path.splitext(filename.lower())[1]
        is_eml = suffix == ".eml" or content_type.lower() in _EML_CONTENT_TYPES
        if not is_eml:
            return self._failure(
                "INVALID_FILE_TYPE",
                f"File '{filename}' does not appear to be an EML file "
                f"(content type: {content_type}, extension: {suffix or 'none'}). "
                "Only .eml files are supported.",
            )

        if not eml_bytes:
            return self._failure("EMPTY_FILE", f"File '{filename}' is empty.")

        # ── Hashes (from raw bytes) ─────────────────────────────────────────
        hashes = _compute_hashes(eml_bytes)

        # ── Parse EML ──────────────────────────────────────────────────────
        try:
            msg = await _run_sync(
                lambda b: BytesParser(policy=email.policy.compat32).parsebytes(b),
                eml_bytes,
            )
        except Exception as exc:
            return self._failure(
                "PARSE_ERROR",
                f"Failed to parse the EML file: {exc}",
                retryable=False,
            )

        # ── Run all analyses in thread pool ─────────────────────────────────
        envelope = await _run_sync(_extract_envelope, msg)
        auth = await _run_sync(_assess_authentication, msg)
        received = await _run_sync(_analyse_received_chain, msg)
        header_anomalies = await _run_sync(_detect_header_anomalies, msg, envelope)
        body_data = await _run_sync(_analyse_body, msg, max_body_chars)
        urls = await _run_sync(_extract_urls, body_data, envelope)
        attachments = await _run_sync(_inventory_attachments, msg)
        risk_summary = await _run_sync(
            _build_risk_summary,
            envelope, auth, received, header_anomalies, body_data, urls, attachments,
        )

        # Strip internal-only keys from body_data before returning
        body_out = {k: v for k, v in body_data.items() if not k.startswith("_")}

        # ── Assemble result ─────────────────────────────────────────────────
        data: dict = {
            "hashes": hashes,
            "envelope": envelope,
            "authentication": auth,
            "received_chain": received,
            "header_anomalies": header_anomalies,
            "body_analysis": body_out,
            "urls": urls,
            "attachments": attachments,
            "attachment_count": len(attachments),
            "risk_summary": risk_summary,
            "_meta": {
                "file_id": file_id,
                "filename": filename,
                "file_size_bytes": file_size,
                "truncated": file_meta.get("truncated", False),
            },
        }

        elapsed = int((time.monotonic() - start) * 1000)

        # ── Offload large results to MinIO ──────────────────────────────────
        try:
            result_json = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            result_json = json.dumps({"error": "Result serialization failed"})

        if len(result_json.encode()) > _MAX_INLINE_BYTES:
            safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
            stored_filename = f"eml_analysis_{safe_name}.json"
            file_info: Optional[dict] = None
            try:
                from src.shared.database import _get_session_maker  # noqa: PLC0415

                async with _get_session_maker()() as db_session:
                    file_info = await self._store_file(
                        data=result_json.encode("utf-8"),
                        filename=stored_filename,
                        content_type="application/json",
                        agent_context=agent_context,
                        session=db_session,
                        description=f"EML security analysis for {filename}",
                    )
            except Exception as exc:
                logger.error("eml_analyzer: failed to store large result: %s", exc)

            summary_data: dict = {
                "_meta": data["_meta"],
                "hashes": data["hashes"],
                "risk_summary": data["risk_summary"],
                "envelope": {
                    k: v for k, v in data["envelope"].items()
                    if k in ("from_", "from", "reply_to", "subject", "date", "message_id")
                },
                "authentication": data["authentication"],
                "attachment_count": data["attachment_count"],
                "url_count": len(urls),
                "note": (
                    "Full analysis stored in the linked file. "
                    "Risk summary and key findings are shown inline."
                ),
            }
            if file_info:
                summary_data["result_file"] = file_info

            return self._partial(
                summary_data,
                code="RESULT_OFFLOADED",
                message=(
                    "Full EML analysis stored externally. "
                    "Risk summary and envelope are shown inline."
                ),
                execution_time_ms=elapsed,
            )

        return self._success(data, execution_time_ms=elapsed)
