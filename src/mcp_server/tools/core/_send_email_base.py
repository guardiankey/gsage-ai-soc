"""gSage AI — Shared base class for send-email tools.

Both :class:`~src.mcp_server.tools.core.send_email.SendEmailTool` (with
human-in-the-loop approval) and
:class:`~src.mcp_server.tools.core.send_email_direct.SendEmailDirectTool`
(without approval) inherit from :class:`_SendEmailBase` to avoid code
duplication.  Subclasses override only the identity / policy attributes
(``name``, ``summary``, ``permissions``, ``requires_approval`` …).

Features provided by the base:

* **SMTP config override** per tool (env ``TOOL_<NAME>__SMTP_*`` or DB).
* **Optional TLS cert validation** (``smtp_validate_certs``) for internal
  relays using self-signed certificates.
* **Attachments by ``file_id``** — loaded from MinIO via
  :meth:`BaseTool._load_file`.  Max 10 files, 10 MB each, 25 MB total.
  Bytes are loaded in memory and released after send.
* **Regex allowlist** on recipients
  (``allowed_recipients_regex`` config) with automatic fallback to the
  current user's own addresses (primary + secondary).  Invalid / rejected
  recipients are reported back to the agent; the email is still sent to
  valid recipients (partial-success semantics).
"""

from __future__ import annotations

import re
import time
import types
from typing import ClassVar, Optional

import aiosmtplib
from sqlalchemy import select as _select

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.models.user import GSageUser
from src.shared.security.context import AgentContext
from src.shared.services.email_service import EmailAttachment, send_email

# Hard limits (not configurable by user — guard rails against LLM abuse).
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB per file
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB total

# Simple RFC 5322-ish email address pattern (deliberately lenient).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_address(addr: str) -> bool:
    return bool(_EMAIL_RE.match(addr.strip()))


def _normalize_addresses(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    addrs: list[str] = [value] if isinstance(value, str) else list(value)
    return [a.strip() for a in addrs if a.strip()] or None


def _split_user_emails(user: GSageUser) -> list[str]:
    """Return the user's owned addresses (primary + secondary), lower-cased."""
    emails: list[str] = []
    if user.email:
        emails.append(user.email.strip().lower())
    if user.secondary_emails:
        for line in user.secondary_emails.splitlines():
            alt = line.strip().lower()
            if alt:
                emails.append(alt)
    return emails


class _SendEmailBase(BaseTool):
    """Shared implementation for the ``send_email`` and ``send_email_direct`` tools."""

    # ── Identity (overridden by subclasses) ─────────────────────────────
    name: ClassVar[str] = "_send_email_base"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Base class — do not use directly."
    category: ClassVar[str] = "email"
    permissions: ClassVar[list[str]] = ["email:send"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
    #: Prevent auto-discovery of this abstract base by the tool registry.
    available: ClassVar[bool] = False

    #: When True and ``allowed_recipients_regex`` is empty, the tool
    #: restricts delivery to the current user's own addresses.  The
    #: ``send_email_direct`` variant sets this to True.
    restrict_to_user_when_no_allowlist: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {"target_entities": "to"}

    # ── Parameters exposed to the LLM ───────────────────────────────────
    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "to": {
                "description": "Recipient e-mail address(es).",
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1},
                ],
            },
            "subject": {
                "type": "string",
                "description": "E-mail subject line.",
                "minLength": 1,
                "maxLength": 998,
            },
            "body": {
                "type": "string",
                "description": "Body of the e-mail (plain text or Markdown).",
                "minLength": 1,
            },
            "cc": {
                "description": "CC recipient(s) — optional.",
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "bcc": {
                "description": "BCC recipient(s) — optional.",
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "attachments": {
                "type": "array",
                "description": (
                    "Optional list of file IDs to attach.  File IDs can come "
                    "from the conversation scope (uploaded by the user), the "
                    "zip tool, generate_document, or any other tool that "
                    "stores files.  Hard limits: max 10 files, 10 MB each, "
                    "25 MB total."
                ),
                "items": {"type": "string"},
                "maxItems": MAX_ATTACHMENTS,
            },
            "content_format": {
                "type": "string",
                "enum": ["auto", "html", "text"],
                "default": "auto",
                "description": (
                    "Body rendering format. "
                    "``auto`` (default) — plain text unless Markdown syntax is detected; "
                    "``html`` — always render as HTML; "
                    "``text`` — plain text only."
                ),
            },
        },
        "required": ["to", "subject", "body"],
        "additionalProperties": False,
    }

    # ── Per-tool SMTP config (optional, overrides system defaults) ──────
    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "smtp_host": {
                "type": "string",
                "description": "SMTP server hostname (overrides SMTP_HOST).",
            },
            "smtp_port": {
                "type": "integer",
                "description": "SMTP port (overrides SMTP_PORT).",
                "minimum": 1,
                "maximum": 65535,
            },
            "smtp_username": {
                "type": "string",
                "description": "SMTP authentication username (overrides SMTP_USERNAME).",
            },
            "smtp_password": {
                "type": "string",
                "description": "SMTP authentication password (overrides SMTP_PASSWORD).",
            },
            "smtp_use_tls": {
                "type": "boolean",
                "description": "Use STARTTLS for the SMTP connection (overrides SMTP_USE_TLS).",
            },
            "smtp_validate_certs": {
                "type": "boolean",
                "description": (
                    "Validate TLS server certificate (default true).  Set "
                    "to false only for internal relays using self-signed "
                    "certificates."
                ),
            },
            "smtp_from_email": {
                "type": "string",
                "description": "Sender address in the From header (overrides SMTP_FROM_EMAIL).",
            },
            "smtp_from_name": {
                "type": "string",
                "description": "Sender display name in the From header (overrides SMTP_FROM_NAME).",
            },
            "smtp_default_format": {
                "type": "string",
                "enum": ["html", "text"],
                "description": "Default body format (overrides SMTP_DEFAULT_FORMAT).",
            },
            "allowed_recipients_regex": {
                "type": "array",
                "description": (
                    "Optional allowlist of regex patterns.  A recipient is "
                    "accepted when it matches at least one pattern or when "
                    "it is one of the current user's own addresses.  When "
                    "empty, the behaviour depends on the tool variant: "
                    "``send_email`` accepts any recipient (approval already "
                    "gates the action); ``send_email_direct`` restricts "
                    "delivery to the current user's own addresses."
                ),
                "items": {"type": "string"},
            },
        },
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {}

    # ── No persistent state needed ─────────────────────────────────────
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────

    async def _fetch_user_emails(self, agent_context: AgentContext) -> list[str]:
        """Return the current user's own email addresses (primary + secondary)."""
        from src.mcp_server.tools.base import _tool_session_ctx
        from src.shared.database import _get_session_maker

        async def _query(session) -> list[str]:
            user = (
                await session.execute(
                    _select(GSageUser).where(GSageUser.id == agent_context.user_id)
                )
            ).scalar_one_or_none()
            return _split_user_emails(user) if user is not None else []

        ctx_session = _tool_session_ctx.get()
        if ctx_session is not None:
            return await _query(ctx_session)
        async with _get_session_maker()() as session:
            return await _query(session)

    def _filter_recipients(
        self,
        addresses: list[str],
        patterns: list[re.Pattern],
        user_emails: set[str],
        allow_any: bool,
    ) -> tuple[list[str], list[dict]]:
        """Split *addresses* into (accepted, rejected).

        ``rejected`` is a list of ``{"email": ..., "reason": ...}`` dicts.
        """
        accepted: list[str] = []
        rejected: list[dict] = []

        for addr in addresses:
            lower = addr.strip().lower()
            if lower in user_emails:
                accepted.append(addr)
                continue
            if allow_any and not patterns:
                accepted.append(addr)
                continue
            if any(p.search(addr) for p in patterns):
                accepted.append(addr)
                continue
            rejected.append(
                {
                    "email": addr,
                    "reason": (
                        "Not in allowed_recipients_regex and not one of the "
                        "user's own addresses."
                    ),
                }
            )

        return accepted, rejected

    async def _load_attachments(
        self,
        file_ids: list[str],
        agent_context: AgentContext,
    ) -> tuple[list[EmailAttachment], list[dict], list[dict]]:
        """Load attachments from MinIO.

        Returns ``(email_attachments, attachment_meta, skipped)``:
          * ``email_attachments`` — ready to pass to :func:`send_email`.
          * ``attachment_meta`` — list of ``{file_id, filename, size_bytes}``
            dicts for the tool result.
          * ``skipped`` — list of ``{file_id, reason}`` dicts.
        """
        email_attachments: list[EmailAttachment] = []
        meta: list[dict] = []
        skipped: list[dict] = []

        total_bytes = 0
        dept_id = (
            str(agent_context.dept_id) if agent_context.dept_id is not None else None
        )

        for fid in file_ids[:MAX_ATTACHMENTS]:
            loaded = await self._load_file(
                file_id=fid,
                org_id=str(agent_context.org_id),
                user_id=str(agent_context.user_id),
                dept_id=dept_id,
                max_bytes=MAX_ATTACHMENT_BYTES,
            )
            if loaded is None:
                skipped.append({"file_id": fid, "reason": "not found or access denied"})
                continue
            if loaded.get("truncated"):
                skipped.append(
                    {
                        "file_id": fid,
                        "reason": (
                            f"file exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB "
                            "per-attachment limit"
                        ),
                    }
                )
                continue
            size = int(loaded["size_bytes"])
            if total_bytes + size > MAX_TOTAL_ATTACHMENT_BYTES:
                skipped.append(
                    {
                        "file_id": fid,
                        "reason": (
                            f"total attachment size would exceed "
                            f"{MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB"
                        ),
                    }
                )
                continue

            total_bytes += size
            email_attachments.append(
                EmailAttachment(
                    filename=loaded["filename"],
                    content_type=loaded["content_type"],
                    data=loaded["data"],
                    delete_after_send=False,
                )
            )
            meta.append(
                {
                    "file_id": loaded["file_id"],
                    "filename": loaded["filename"],
                    "size_bytes": size,
                }
            )

        # Report any file IDs dropped because they exceeded the count cap.
        for fid in file_ids[MAX_ATTACHMENTS:]:
            skipped.append(
                {
                    "file_id": fid,
                    "reason": f"exceeded max {MAX_ATTACHMENTS} attachments per email",
                }
            )

        return email_attachments, meta, skipped

    def _build_smtp_override(self, config: dict) -> dict:
        """Translate tool config keys into the ``SmtpConfig`` override dict."""
        override: dict = {}
        if config.get("smtp_host"):
            override["host"] = config["smtp_host"]
        if config.get("smtp_port"):
            override["port"] = int(config["smtp_port"])
        if config.get("smtp_username"):
            override["username"] = config["smtp_username"]
        if config.get("smtp_password"):
            override["password"] = config["smtp_password"]
        if "smtp_use_tls" in config:
            override["use_tls"] = bool(config["smtp_use_tls"])
        if "smtp_validate_certs" in config:
            override["validate_certs"] = bool(config["smtp_validate_certs"])
        if config.get("smtp_from_email"):
            override["from_email"] = config["smtp_from_email"]
        if config.get("smtp_from_name"):
            override["from_name"] = config["smtp_from_name"]
        if config.get("smtp_default_format"):
            override["default_format"] = config["smtp_default_format"]
        return override

    # ────────────────────────────────────────────────────────────────────
    # execute
    # ────────────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.perf_counter()

        # ── 1. Normalise & syntactically validate recipients ────────────
        to_list = _normalize_addresses(params["to"])
        if not to_list:
            return self._failure(
                "INVALID_PARAMS",
                "Nenhum destinatário válido fornecido em 'to'.",
            )

        invalid = [a for a in to_list if not _validate_address(a)]
        if invalid:
            return self._failure(
                "INVALID_EMAIL_ADDRESS",
                f"Endereço(s) de e-mail inválido(s): {', '.join(invalid)}",
            )

        cc_list = _normalize_addresses(params.get("cc")) or []
        bcc_list = _normalize_addresses(params.get("bcc")) or []

        for addr_list, field_name in ((cc_list, "cc"), (bcc_list, "bcc")):
            bad = [a for a in addr_list if not _validate_address(a)]
            if bad:
                return self._failure(
                    "INVALID_EMAIL_ADDRESS",
                    f"Endereço(s) inválido(s) em '{field_name}': {', '.join(bad)}",
                )

        subject: str = params["subject"]
        body: str = params["body"]
        content_format: str = params.get("content_format", "auto")
        attachment_ids_raw = params.get("attachments") or []
        attachment_ids = [str(x).strip() for x in attachment_ids_raw if str(x).strip()]

        # ── 2. Apply regex allowlist (with user-own-emails fallback) ───
        raw_patterns = config.get("allowed_recipients_regex") or []
        compiled_patterns: list[re.Pattern] = []
        for pat in raw_patterns:
            try:
                compiled_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as exc:
                return self._failure(
                    "INVALID_CONFIG",
                    f"Regex inválida em allowed_recipients_regex: {pat!r} — {exc}",
                    retryable=False,
                )

        user_emails_list = await self._fetch_user_emails(agent_context)
        user_emails: set[str] = set(user_emails_list)

        # Policy: when no allowlist is configured, ``send_email`` (the
        # approval-gated variant) accepts any recipient; ``send_email_direct``
        # restricts to the user's own addresses.
        allow_any = not self.restrict_to_user_when_no_allowlist

        accepted_to, rejected_to = self._filter_recipients(
            to_list, compiled_patterns, user_emails, allow_any
        )
        accepted_cc, rejected_cc = self._filter_recipients(
            cc_list, compiled_patterns, user_emails, allow_any
        )
        accepted_bcc, rejected_bcc = self._filter_recipients(
            bcc_list, compiled_patterns, user_emails, allow_any
        )
        rejected = [
            *({"field": "to", **r} for r in rejected_to),
            *({"field": "cc", **r} for r in rejected_cc),
            *({"field": "bcc", **r} for r in rejected_bcc),
        ]

        if not accepted_to:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return self._failure(
                "NO_VALID_RECIPIENTS",
                (
                    "Nenhum destinatário em 'to' é permitido pela política de "
                    "envio desta ferramenta (allowed_recipients_regex) e "
                    "nenhum é um endereço próprio do usuário."
                ),
                retryable=False,
                execution_time_ms=elapsed,
            )

        # ── 3. Load attachments from MinIO ─────────────────────────────
        email_attachments: list[EmailAttachment] = []
        attachment_meta: list[dict] = []
        attachment_skipped: list[dict] = []
        if attachment_ids:
            email_attachments, attachment_meta, attachment_skipped = (
                await self._load_attachments(attachment_ids, agent_context)
            )

        # ── 4. Build SMTP override ─────────────────────────────────────
        smtp_override = self._build_smtp_override(config)
        fake_org = types.SimpleNamespace(
            smtp_config=smtp_override if smtp_override else None
        )

        # ── 5. Send ─────────────────────────────────────────────────────
        try:
            await send_email(
                to=accepted_to,
                subject=subject,
                body_text=body,
                cc=accepted_cc or None,
                bcc=accepted_bcc or None,
                attachments=email_attachments or None,
                content_format=content_format,
                org=fake_org,
            )
        except RuntimeError as exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return self._failure(
                "SMTP_NOT_CONFIGURED",
                str(exc),
                retryable=False,
                execution_time_ms=elapsed,
            )
        except aiosmtplib.SMTPException as exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return self._failure(
                "SMTP_ERROR",
                f"Erro SMTP ao enviar e-mail: {exc}",
                retryable=True,
                execution_time_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - t0) * 1000)
            return self._failure(
                "SEND_FAILED",
                f"Falha ao enviar e-mail: {exc}",
                retryable=True,
                execution_time_ms=elapsed,
            )
        finally:
            # Release in-memory attachment bytes as soon as possible.
            for att in email_attachments:
                att.data = None

        # ── 6. Success / partial-success result ────────────────────────
        elapsed = int((time.perf_counter() - t0) * 1000)
        data = {
            "delivered_to": accepted_to,
            "subject": subject,
            "cc": accepted_cc,
            "bcc": accepted_bcc,
            "content_format": content_format,
            "attachments": attachment_meta,
            "attachments_skipped": attachment_skipped,
            "rejected": rejected,
        }
        if rejected or attachment_skipped:
            return ToolResult.partial(
                data=data,
                code="PARTIAL_DELIVERY",
                message=(
                    "E-mail enviado, porém alguns destinatários e/ou anexos "
                    "foram rejeitados. Verifique 'rejected' e "
                    "'attachments_skipped' no payload."
                ),
                retryable=False,
                tool_name=self.name,
                version=self.version,
                execution_time_ms=elapsed,
            )
        return self._success(data, execution_time_ms=elapsed)
