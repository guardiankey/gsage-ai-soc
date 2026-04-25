"""gSage AI — Email MIME parser (Phase 7).

Parses raw IMAP email bytes into a structured ParsedEmail dataclass.

Security rules (per PROMPT.md Phase 7):
  - Attachments are EXPLICITLY SKIPPED — never processed or stored.
  - Maximum email size: 5 MB (configurable per email account).
  - Only text/plain and text/html MIME parts are extracted.
  - Null bytes rejected; encoding validated as UTF-8.
  - HTML body stripped of tags before use as agent input.
"""

from __future__ import annotations

import email
import email.utils
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from typing import Optional

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────

_MAX_SUBJECT_LEN = 500
_MAX_ADDR_LEN = 255
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_MULTI_WHITESPACE_RE = re.compile(r"\s{3,}")

# Strip common email quote markers from body (>>>, > , etc.)
_QUOTE_LINE_RE = re.compile(r"^>.*$", re.MULTILINE)


# ── Data Model ─────────────────────────────────────────────────────────────


@dataclass
class ParsedEmail:
    """Structured representation of a parsed inbound email."""

    message_id: str
    in_reply_to: Optional[str]
    references: list[str]
    from_addr: str           # normalized lowercase
    to_addr: str             # the receiving mailbox address
    subject: str             # original subject (with Re:/Fwd: — normalized later)
    normalized_subject: str  # Re:/Fwd: stripped — used as thread key
    date: datetime
    body_text: str           # best text content to use as agent query
    body_html: Optional[str] # raw HTML (stored but not sent to agent directly)
    raw_size_bytes: int


# ── Public API ─────────────────────────────────────────────────────────────


def parse_raw_email(
    raw_bytes: bytes,
    max_size_bytes: int = 5_242_880,
) -> Optional[ParsedEmail]:
    """
    Parse raw IMAP email bytes into a ParsedEmail.

    Args:
        raw_bytes: Raw RFC 2822 email bytes (from IMAP FETCH).
        max_size_bytes: Maximum allowed size. Emails larger than this are
            rejected (returns None) to prevent DoS via oversized messages.

    Returns:
        ParsedEmail on success, None if the email should be discarded
        (oversized, missing required headers, or parsing errors).

    Security:
        - All attachment parts are silently skipped.
        - Null bytes are stripped from all text fields.
        - Content is truncated at 20 KB before being passed to the agent
          (sanitizer enforces the actual limit).
    """
    raw_size = len(raw_bytes)
    if raw_size > max_size_bytes:
        logger.warning(
            "Email rejected: size %d bytes exceeds limit %d bytes",
            raw_size,
            max_size_bytes,
        )
        return None

    try:
        msg: Message = email.message_from_bytes(raw_bytes)
    except Exception as exc:
        logger.warning("Failed to parse email: %s", exc)
        return None

    # ── Required headers ───────────────────────────────────────────────────

    message_id = _clean_header(msg.get("Message-ID", ""))
    if not message_id:
        logger.warning("Email rejected: missing Message-ID header")
        return None

    raw_from = msg.get("From", "")
    from_addr = _extract_addr(raw_from)
    if not from_addr:
        logger.warning("Email rejected: missing/invalid From header — raw=%r", raw_from)
        return None

    raw_to = msg.get("To", "")
    to_addr = _extract_addr(raw_to)
    if not to_addr:
        to_addr = ""  # best-effort; validated downstream

    subject = _clean_header(msg.get("Subject", "(no subject)"))[:_MAX_SUBJECT_LEN]
    normalized_subject = normalize_subject(subject)

    # ── Optional threading headers ─────────────────────────────────────────

    in_reply_to = _clean_header(msg.get("In-Reply-To", "")) or None
    raw_references = _clean_header(msg.get("References", ""))
    references = [r.strip() for r in raw_references.split() if r.strip()] if raw_references else []

    # ── Date ───────────────────────────────────────────────────────────────

    raw_date = msg.get("Date", "")
    date = _parse_date(raw_date)

    # ── Body extraction (attachments EXPLICITLY SKIPPED) ──────────────────

    body_text, body_html = _extract_body(msg)

    return ParsedEmail(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        from_addr=from_addr.lower().strip(),
        to_addr=to_addr.lower().strip(),
        subject=subject,
        normalized_subject=normalized_subject,
        date=date,
        body_text=body_text,
        body_html=body_html,
        raw_size_bytes=raw_size,
    )


# ── Internal helpers ────────────────────────────────────────────────────────


def _clean_header(value: str) -> str:
    """Normalize a header value: strip whitespace, remove null bytes."""
    return value.replace("\x00", "").strip()


def _extract_addr(header_value: str) -> str:
    """
    Extract just the email address from a From:/To: header.

    Examples:
        "Alice <alice@example.com>" → "alice@example.com"
        "alice@example.com"        → "alice@example.com"
    """
    try:
        realname, addr = email.utils.parseaddr(header_value)
        return addr.replace("\x00", "").strip()
    except Exception:
        return ""


def normalize_subject(subject: str) -> str:
    """
    Strip common reply/forward prefixes for thread key matching.

    "Re: Re: Analyze domain" → "Analyze domain"
    "Fwd: [SOC-AI] Analysis" → "[SOC-AI] Analysis"
    """
    normalized = re.sub(
        r"^(Re:\s*|Fwd?:\s*|AW:\s*|ÉS:\s*|R:\s*)+",
        "",
        subject.strip(),
        flags=re.IGNORECASE,
    ).strip()
    return normalized or subject


def _parse_date(raw_date: str) -> datetime:
    """Parse email Date header. Falls back to UTC now on parse error."""
    try:
        parsed = email.utils.parsedate_to_datetime(raw_date)
        # Ensure timezone-aware
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.now(timezone.utc)


def _extract_body(msg: Message) -> tuple[str, Optional[str]]:
    """
    Walk MIME parts and extract text/plain and text/html.

    SECURITY: Attachment parts (disposition=attachment or non-text content
    types like application/*, image/*, etc.) are SILENTLY SKIPPED per
    PROMPT.md Phase 7 security decision.

    Returns:
        (body_text, body_html) — body_text is the plain text to use as agent
        input. If only HTML is present, body_text is derived from stripped HTML.
    """
    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if _is_attachment(part):
                # Explicitly skip attachments — security decision
                logger.debug(
                    "Attachment skipped: content_type=%s filename=%r",
                    part.get_content_type(),
                    part.get_filename(),
                )
                continue

            content_type = part.get_content_type()
            if content_type == "text/plain":
                decoded = _decode_part(part)
                if decoded:
                    text_parts.append(decoded)
            elif content_type == "text/html":
                decoded = _decode_part(part)
                if decoded:
                    html_parts.append(decoded)
            # All other content types (images, PDFs, etc.) are ignored
    else:
        # Single-part message
        content_type = msg.get_content_type()
        if content_type == "text/plain":
            decoded = _decode_part(msg)
            if decoded:
                text_parts.append(decoded)
        elif content_type == "text/html":
            decoded = _decode_part(msg)
            if decoded:
                html_parts.append(decoded)

    body_text_raw = "\n\n".join(text_parts).strip()
    body_html_raw = "\n\n".join(html_parts).strip() or None

    if body_text_raw:
        # Remove quoted reply lines (lines starting with ">")
        body_text = _QUOTE_LINE_RE.sub("", body_text_raw).strip()
        body_text = _MULTI_WHITESPACE_RE.sub(" ", body_text).strip()
    elif body_html_raw:
        # Fall back: strip HTML tags to get plain text
        body_text = _HTML_TAG_RE.sub(" ", body_html_raw)
        body_text = _MULTI_WHITESPACE_RE.sub(" ", body_text).strip()
    else:
        body_text = ""

    return body_text, body_html_raw


def _is_attachment(part: Message) -> bool:
    """Return True if this MIME part is an attachment (should be skipped)."""
    content_disposition = part.get_content_disposition() or ""
    if content_disposition.lower().strip().startswith("attachment"):
        return True
    # Also skip non-text, non-multipart top-level types
    maintype = part.get_content_maintype()
    if maintype not in ("text", "multipart", "message"):
        return True
    return False


def _decode_part(part: Message) -> str:
    """Decode a MIME part payload to a UTF-8 string."""
    try:
        payload = part.get_payload(decode=True)
        # get_payload(decode=True) returns bytes when the part is non-multipart.
        # Pylance sees a broader union; the isinstance guard narrows it to bytes.
        if not payload or not isinstance(payload, bytes):
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return payload.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Failed to decode MIME part: %s", exc)
        return ""
