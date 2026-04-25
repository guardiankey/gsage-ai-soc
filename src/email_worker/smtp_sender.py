"""gSage AI — SMTP reply sender (Phase 7).

Sends the AI agent's response back to the original sender via SMTP.

Features per PROMPT.md Phase 7:
  - TLS (STARTTLS on port 587 or implicit SSL on port 465).
  - Proper email threading headers (In-Reply-To, References).
  - Optional subject_prefix and reply_footer from email account config.
  - Generates a unique Message-ID for the outbound email.
  - Returns the outbound Message-ID so the caller can store it in DB.

Uses ``aiosmtplib`` for non-blocking SMTP over asyncio.
"""

from __future__ import annotations

import email.utils
import logging
import time
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import aiosmtplib  # type: ignore[import-untyped]
import markdown as md_lib  # type: ignore[import-untyped]

from src.shared.models.email_account import GSageEmailAccount

logger = logging.getLogger(__name__)

# Hard limit on outbound body size (prevent accidentally sending huge replies).
_MAX_BODY_BYTES = 1_048_576  # 1 MB

# Minimal CSS injected into the HTML part to ensure readability across email clients.
_HTML_STYLE = """
<style>
  body { font-family: Arial, sans-serif; font-size: 14px; color: #222; line-height: 1.6; max-width: 720px; margin: 0 auto; padding: 16px; }
  h1, h2, h3 { color: #1a1a2e; margin-top: 1.2em; }
  code { background: #f4f4f4; border-radius: 3px; padding: 2px 5px; font-family: monospace; font-size: 13px; }
  pre { background: #f4f4f4; border-radius: 4px; padding: 12px; overflow-x: auto; }
  pre code { background: none; padding: 0; }
  blockquote { border-left: 3px solid #ccc; margin: 0; padding-left: 12px; color: #555; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
  th { background: #f0f0f0; }
  hr { border: none; border-top: 1px solid #ddd; margin: 16px 0; }
  a { color: #0066cc; }
</style>
"""


def _markdown_to_html(text: str) -> str:
    """Convert Markdown text to a complete HTML email body.

    Uses the ``markdown`` library with the ``tables`` and ``fenced_code``
    extensions, then wraps the output in a minimal styled HTML document.
    """
    body_html = md_lib.markdown(
        text,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>{_HTML_STYLE}</head>"
        f"<body>{body_html}</body></html>"
    )


async def send_reply(
    account: GSageEmailAccount,
    *,
    to_addr: str,
    subject: str,
    body_text: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> str:
    """Send a reply email and return the outbound Message-ID.

    Args:
        account:     Email account to use for SMTP credentials + From address.
        to_addr:     Recipient address (the original sender).
        subject:     Subject of the original email (``Re:`` is prepended here).
        body_text:   Plain-text body (agent's response).
        in_reply_to: Original email's Message-ID for In-Reply-To header.
        references:  Space-separated References header chain.

    Returns:
        The Message-ID of the sent email (``<uuid@domain>`` format).

    Raises:
        aiosmtplib.SMTPException: on SMTP-level send failure.
    """
    if len(body_text.encode("utf-8")) > _MAX_BODY_BYTES:
        body_text = body_text[: _MAX_BODY_BYTES // 4] + "\n\n[… response truncated …]"

    outbound_message_id = _generate_message_id(account.email)
    out_subject = _build_subject(subject, account.subject_prefix)
    from_header = f"{account.sender_name} <{account.email}>"

    # Append optional footer.
    full_body = body_text
    if account.reply_footer:
        full_body = f"{body_text}\n\n--\n{account.reply_footer}"

    # Build multipart/alternative with plain-text and HTML parts.
    # The HTML part is generated from the Markdown body so that clients
    # that support HTML get formatted output; plain-text clients get the
    # original Markdown source (which is already readable as-is).
    msg = MIMEMultipart("alternative")
    msg["From"] = from_header
    msg["To"] = to_addr
    msg["Subject"] = out_subject
    msg["Message-ID"] = outbound_message_id
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["X-Mailer"] = "gSage AI"

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # Build References: append outbound message to existing chain.
        if references:
            msg["References"] = f"{references} {in_reply_to}"
        else:
            msg["References"] = in_reply_to

    # Plain-text part comes first; HTML part last (preferred by RFC 2046).
    msg.attach(MIMEText(full_body, "plain", "utf-8"))
    msg.attach(MIMEText(_markdown_to_html(full_body), "html", "utf-8"))

    # ── SMTP send ─────────────────────────────────────────────────────────
    use_tls = account.smtp_use_tls
    port = account.smtp_port
    # Port 465 → implicit SSL; port 587 → STARTTLS.
    use_starttls = use_tls and (port == 587)
    use_ssl = use_tls and (port == 465)

    logger.info(
        "smtp_sender.send_reply: sending — to=%s subject=%s account=%s message_id=%s",
        to_addr,
        out_subject,
        account.email,
        outbound_message_id,
    )

    smtp_kwargs: dict = {
        "hostname": account.smtp_host,
        "port": port,
        "use_tls": use_ssl,
        "validate_certs": account.smtp_verify_ssl,
    }
    # Only authenticate when credentials are configured (username = '' means relay).
    if account.smtp_username:
        smtp_kwargs["username"] = account.smtp_username
        smtp_kwargs["password"] = account.smtp_password
    if use_starttls:
        smtp_kwargs["start_tls"] = True

    await aiosmtplib.send(msg, **smtp_kwargs)

    logger.info(
        "smtp_sender.send_reply: sent — message_id=%s to=%s",
        outbound_message_id,
        to_addr,
    )
    return outbound_message_id


# ── Helpers ───────────────────────────────────────────────────────────────


def _generate_message_id(from_addr: str) -> str:
    """Generate a unique RFC 5322 Message-ID."""
    domain = from_addr.split("@")[-1] if "@" in from_addr else "gsage.local"
    unique = uuid.uuid4().hex
    ts = int(time.time())
    return f"<{ts}.{unique}@{domain}>"


def _build_subject(original_subject: str, prefix: Optional[str] = None) -> str:
    """Prepend Re: (once) and optional prefix to the subject."""
    # Strip existing Re: / RE: to avoid "Re: Re: Re:" chains.
    cleaned = original_subject.strip()
    import re
    cleaned = re.sub(r"^(re|RE|Re):\s*", "", cleaned).strip()

    if prefix:
        return f"{prefix} Re: {cleaned}"
    return f"Re: {cleaned}"
