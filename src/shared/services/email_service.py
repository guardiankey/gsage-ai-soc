"""gSage AI — Shared async email service.

Provides a single :func:`send_email` coroutine usable by ``backend_api``,
``mcp_server``, and any other in-process service.

SMTP configuration is resolved by :func:`resolve_smtp_config`:

1. Global defaults from :class:`~src.shared.config.settings.Settings`.
2. Optionally overridden per-organisation via
   :attr:`~src.shared.models.organization.GSageOrganization.smtp_config`
   (AES-256-GCM encrypted JSONB column).

Body formatting
---------------
``content_format`` controls how the email body is rendered:

* ``"text"`` — plain-text only, no HTML part.
* ``"html"`` — HTML preferred.  If ``body_html`` is *not* supplied the
  service auto-generates it:  Markdown-ish text is converted via
  ``markdown-it-py``; otherwise the plain text is wrapped in ``<pre>``.
* ``"auto"`` (default) — same as ``"html"`` but if the body looks like
  plain prose (no Markdown indicators) it falls back to ``"text"`` only
  to keep emails lightweight.

Attachments
-----------
Pass :class:`EmailAttachment` instances.  Limits: 10 MB per file,
25 MB total.  Files with ``delete_after_send=True`` (the default) and
paths under ``/tmp`` are deleted after a successful send.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import ssl
from dataclasses import dataclass, field
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Union

import aiosmtplib

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Size limits
# ---------------------------------------------------------------------------

_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024   # 10 MB each
_MAX_TOTAL_BYTES = 25 * 1024 * 1024        # 25 MB total

# ---------------------------------------------------------------------------
# HTML wrapper for rich email rendering
# ---------------------------------------------------------------------------

_EMAIL_HTML_WRAPPER = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    color: #333;
    max-width: 700px;
    margin: 0 auto;
    padding: 20px;
  }}
  pre {{
    background: #f4f4f4;
    border-left: 3px solid #ccc;
    padding: 12px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-wrap: break-word;
  }}
  code {{
    background: #f4f4f4;
    padding: 2px 4px;
    border-radius: 3px;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 90%;
  }}
  blockquote {{
    border-left: 4px solid #ddd;
    margin: 0;
    padding-left: 16px;
    color: #666;
  }}
  table {{
    border-collapse: collapse;
    width: 100%;
  }}
  th, td {{
    border: 1px solid #ddd;
    padding: 6px 10px;
    text-align: left;
  }}
  th {{
    background: #f0f0f0;
  }}
  a {{ color: #0066cc; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Markdown detection and conversion
# ---------------------------------------------------------------------------

_MD_PATTERNS = [
    re.compile(r"^#{1,6}\s+\S", re.MULTILINE),          # ATX headings
    re.compile(r"^\s*[-*+]\s+\S", re.MULTILINE),         # unordered list
    re.compile(r"^\s*\d+\.\s+\S", re.MULTILINE),         # ordered list
    re.compile(r"```"),                                    # fenced code block
    re.compile(r"`[^`\n]+`"),                              # inline code
    re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__"),          # bold
    re.compile(r"\[.+?\]\(.+?\)"),                        # link
    re.compile(r"^\s*>\s+\S", re.MULTILINE),              # blockquote
    re.compile(r"^\s*\|.+\|", re.MULTILINE),              # table row
]

_SAMPLE_LINES = 30


def _looks_like_markdown(text: str) -> bool:
    """Heuristic: return ``True`` if *text* contains ≥ 2 Markdown patterns."""
    sample = "\n".join(text.splitlines()[:_SAMPLE_LINES])
    hits = sum(1 for p in _MD_PATTERNS if p.search(sample))
    return hits >= 2


def _markdown_to_html(text: str) -> str:
    """Convert Markdown *text* to an HTML string (uses ``markdown-it-py``)."""
    try:
        from markdown_it import MarkdownIt  # type: ignore[import-untyped]
    except ImportError:
        # Fallback: wrap in <pre> — markdown-it-py should always be present
        return _plaintext_to_html(text)

    _md = MarkdownIt("commonmark")
    body_html = _md.render(text)
    return _EMAIL_HTML_WRAPPER.format(body=body_html)


def _plaintext_to_html(text: str) -> str:
    """Escape *text* and wrap in ``<pre>`` inside the email HTML wrapper."""
    try:
        from markupsafe import escape  # type: ignore[import-untyped]
        escaped = str(escape(text))
    except ImportError:
        escaped = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
    body_html = f'<pre style="white-space:pre-wrap">{escaped}</pre>'
    return _EMAIL_HTML_WRAPPER.format(body=body_html)


def _prepare_body_parts(
    body_text: str,
    body_html: Optional[str],
    content_format: str,
) -> tuple[str, Optional[str]]:
    """Return ``(plain_text, html_or_None)`` based on the requested format."""
    if content_format == "text":
        return body_text, None

    # "html" or "auto"
    if body_html:
        return body_text, body_html

    if content_format == "auto":
        if not _looks_like_markdown(body_text):
            return body_text, None  # plain prose — skip HTML part
        return body_text, _markdown_to_html(body_text)

    # content_format == "html"
    if _looks_like_markdown(body_text):
        return body_text, _markdown_to_html(body_text)
    return body_text, _plaintext_to_html(body_text)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    from_email: str
    from_name: str
    default_format: str = "html"  # "html" | "text"
    #: When False, accept self-signed/untrusted TLS certificates.
    validate_certs: bool = True


@dataclass
class EmailAttachment:
    """Describes a file to attach to an outgoing email.

    Two source modes are supported:

    * **In-memory** — set :attr:`data` to the raw bytes; :attr:`path` is
      ignored.  Preferred for tool-loaded attachments (MinIO, zip tool, …).
    * **Filesystem** — set :attr:`path` to a local file; the bytes are read
      at send time.  Files under ``/tmp`` are deleted after send when
      :attr:`delete_after_send` is True.
    """

    path: str = ""
    filename: Optional[str] = None
    content_type: Optional[str] = None
    #: If True (default) and the path is under /tmp, delete after send.
    delete_after_send: bool = True
    #: Optional raw bytes — when set, takes precedence over ``path``.
    data: Optional[bytes] = None


# ---------------------------------------------------------------------------
# Configuration resolution
# ---------------------------------------------------------------------------


def resolve_smtp_config(org=None) -> SmtpConfig:
    """Build a :class:`SmtpConfig` from global settings + optional org override.

    Parameters
    ----------
    org:
        Optional :class:`~src.shared.models.organization.GSageOrganization`
        instance.  When provided, any non-``None`` key in
        ``org.smtp_config`` overrides the global setting.
    """
    from src.shared.config.settings import get_settings

    s = get_settings()
    cfg = SmtpConfig(
        host=s.smtp_host,
        port=s.smtp_port,
        username=s.smtp_username,
        password=s.smtp_password,
        use_tls=s.smtp_use_tls,
        from_email=s.smtp_from_email,
        from_name=s.smtp_from_name,
        default_format=s.smtp_default_format,
        validate_certs=getattr(s, "smtp_validate_certs", True),
    )

    if org is not None:
        override: Optional[dict] = getattr(org, "smtp_config", None)
        if override:
            if "host" in override and override["host"]:
                cfg.host = override["host"]
            if "port" in override and override["port"]:
                cfg.port = int(override["port"])
            if "username" in override and override["username"]:
                cfg.username = override["username"]
            if "password" in override and override["password"]:
                cfg.password = override["password"]
            if "use_tls" in override:
                cfg.use_tls = bool(override["use_tls"])
            if "validate_certs" in override:
                cfg.validate_certs = bool(override["validate_certs"])
            if "from_email" in override and override["from_email"]:
                cfg.from_email = override["from_email"]
            if "from_name" in override and override["from_name"]:
                cfg.from_name = override["from_name"]
            if "default_format" in override and override["default_format"]:
                cfg.default_format = override["default_format"]

    return cfg


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------


def _attach_files(
    outer: MIMEMultipart,
    attachments: list[EmailAttachment],
) -> None:
    """Attach files to *outer* (``MIMEMultipart("mixed")``).

    Supports two source modes per attachment: in-memory bytes
    (:attr:`EmailAttachment.data`) or a filesystem path
    (:attr:`EmailAttachment.path`).  In-memory data takes precedence.
    """
    total = 0
    for att in attachments:
        # ── Resolve bytes + size from either source ─────────────────────
        if att.data is not None:
            data = att.data
            size = len(data)
            source_label = att.filename or "<in-memory>"
        else:
            if not att.path:
                logger.warning("Attachment has neither data nor path; skipping")
                continue
            size = os.path.getsize(att.path)
            source_label = att.path
            data = None  # read later, only if size check passes

        if size > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "Attachment %s is %.1f MB — exceeds 10 MB limit; skipping",
                source_label,
                size / 1024 / 1024,
            )
            continue
        total += size
        if total > _MAX_TOTAL_BYTES:
            logger.warning(
                "Total attachment size exceeded 25 MB; skipping remaining files"
            )
            break

        filename = att.filename or (
            os.path.basename(att.path) if att.path else "attachment.bin"
        )
        ctype = att.content_type or (
            (mimetypes.guess_type(filename)[0] if filename else None)
            or (mimetypes.guess_type(att.path)[0] if att.path else None)
            or "application/octet-stream"
        )

        if data is None:
            with open(att.path, "rb") as fh:
                data = fh.read()

        part = MIMEApplication(data, _subtype=ctype.split("/")[-1])
        part.add_header("Content-Disposition", "attachment", filename=filename)
        outer.attach(part)


def _cleanup_attachments(attachments: list[EmailAttachment]) -> None:
    """Delete temp files marked for cleanup and release in-memory bytes."""
    for att in attachments:
        # Release in-memory bytes so the GC can reclaim them quickly.
        if att.data is not None:
            att.data = None
        if att.path and att.delete_after_send and att.path.startswith("/tmp"):
            try:
                os.remove(att.path)
                logger.debug("Deleted temp attachment: %s", att.path)
            except OSError as exc:
                logger.warning("Could not delete temp attachment %s: %s", att.path, exc)


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------


async def send_email(
    *,
    to: Union[str, list[str]],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    cc: Optional[Union[str, list[str]]] = None,
    bcc: Optional[Union[str, list[str]]] = None,
    attachments: Optional[list[EmailAttachment]] = None,
    content_format: str = "auto",
    org=None,
) -> None:
    """Send an email asynchronously via ``aiosmtplib``.

    Parameters
    ----------
    to:
        One or more recipient addresses.
    subject:
        Email subject line.
    body_text:
        Plain-text body (always included for clients that cannot render HTML).
    body_html:
        Explicit HTML body.  When ``None`` and ``content_format`` is not
        ``"text"``, the service auto-generates HTML from ``body_text``.
    cc:
        CC address(es).  Included in headers and in the SMTP envelope.
    bcc:
        BCC address(es).  Added to the SMTP envelope only — NOT in headers.
    attachments:
        List of :class:`EmailAttachment` instances to include.
    content_format:
        ``"auto"`` | ``"html"`` | ``"text"``  (default: ``"auto"``).
    org:
        Optional :class:`~src.shared.models.organization.GSageOrganization`
        for per-org SMTP config overrides.

    Raises
    ------
    :exc:`RuntimeError`
        When SMTP is not configured (empty host).
    :exc:`aiosmtplib.SMTPException`
        On SMTP protocol errors.
    """
    cfg = resolve_smtp_config(org)

    if not cfg.host:
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_HOST (and other SMTP_* vars) in .env."
        )

    # Normalise recipients
    to_list: list[str] = [to] if isinstance(to, str) else list(to)
    cc_list: list[str] = ([cc] if isinstance(cc, str) else list(cc)) if cc else []
    bcc_list: list[str] = ([bcc] if isinstance(bcc, str) else list(bcc)) if bcc else []

    # Resolve effective format (org override → caller override)
    effective_format = content_format if content_format != "auto" else cfg.default_format
    if content_format == "auto":
        effective_format = "auto"  # keep auto logic in _prepare_body_parts

    plain, html = _prepare_body_parts(body_text, body_html, effective_format)

    # Build the MIME message
    from_addr = f"{cfg.from_name} <{cfg.from_email}>" if cfg.from_name else cfg.from_email
    all_envelope_recipients = to_list + cc_list + bcc_list

    if attachments:
        outer = MIMEMultipart("mixed")
        if html:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(plain, "plain", "utf-8"))
            alt.attach(MIMEText(html, "html", "utf-8"))
            outer.attach(alt)
        else:
            outer.attach(MIMEText(plain, "plain", "utf-8"))
        _attach_files(outer, attachments)
        msg = outer
    elif html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(plain, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    # BCC intentionally omitted from headers

    # Build an unverified SSL context when the admin opted out of cert
    # validation (e.g. internal relays using self-signed certificates).
    send_kwargs: dict = {
        "hostname": cfg.host,
        "port": cfg.port,
        "username": cfg.username or None,
        "password": cfg.password or None,
        "use_tls": cfg.use_tls,
        "recipients": all_envelope_recipients,
        "validate_certs": cfg.validate_certs,
    }
    if not cfg.validate_certs:
        insecure_ctx = ssl.create_default_context()
        insecure_ctx.check_hostname = False
        insecure_ctx.verify_mode = ssl.CERT_NONE
        send_kwargs["tls_context"] = insecure_ctx

    try:
        await aiosmtplib.send(msg, **send_kwargs)
        logger.info(
            "Email sent to=%s subject=%r",
            ", ".join(to_list),
            subject,
        )
    except Exception:
        logger.exception("Failed to send email subject=%r to=%s", subject, ", ".join(to_list))
        raise
    finally:
        if attachments:
            _cleanup_attachments(attachments)


# ---------------------------------------------------------------------------
# High-level: approval notification
# ---------------------------------------------------------------------------


async def send_approval_notification(
    *,
    to_email: str,
    tool_name: str,
    requester_name: str,
    approval_id: str,
    summary: Optional[str] = None,
    org=None,
) -> None:
    """Send an approval-request notification to the delegated approver.

    Parameters
    ----------
    to_email:
        Email address of the user who must approve.
    tool_name:
        Name of the tool whose execution is pending.
    requester_name:
        Display name or email of the user who triggered the action.
    approval_id:
        The Agno approval ID — included in the email for reference.
    summary:
        Human-readable description of the pending action (from the agent).
    org:
        Optional organisation for SMTP config overrides.
    """
    action_text = summary or f"{requester_name} requested execution of '{tool_name}'"

    subject = f"[gSage] Approval required: {tool_name}"

    body_text = (
        f"Hello,\n\n"
        f"An action requires your approval:\n\n"
        f"  {action_text}\n\n"
        f"Approval ID: {approval_id}\n\n"
        f"Please review and approve or reject via the gSage approvals interface.\n\n"
        f"— gSage AI"
    )

    body_html_content = f"""\
<h2>Approval Required</h2>
<p>An action is pending your approval:</p>
<blockquote><strong>{action_text}</strong></blockquote>
<table>
  <tr><th>Tool</th><td>{tool_name}</td></tr>
  <tr><th>Requested by</th><td>{requester_name}</td></tr>
  <tr><th>Approval ID</th><td><code>{approval_id}</code></td></tr>
</table>
<p>Please review and approve or reject via the <strong>gSage approvals interface</strong>.</p>
<p style="color:#888;font-size:12px">— gSage AI</p>"""

    body_html = _EMAIL_HTML_WRAPPER.format(body=body_html_content)

    await send_email(
        to=to_email,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        content_format="html",
        org=org,
    )
