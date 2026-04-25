"""gSage AI — Send Email tool (requires human approval)."""

from __future__ import annotations

from typing import ClassVar

from src.mcp_server.tools.core._send_email_base import _SendEmailBase


class SendEmailTool(_SendEmailBase):
    """MCP tool: send an email via the configured SMTP server (with approval).

    Tool-level SMTP config (stored encrypted in DB) takes precedence over the
    system-wide ``SMTP_*`` environment variables.  Any field left blank in the
    tool config falls back to the global setting.

    The :attr:`allowed_recipients_regex` config restricts the set of
    recipients; when empty, any recipient is accepted because each send is
    already gated by human-in-the-loop approval.

    Attachments may reference any ``file_id`` accessible to the user
    (conversation uploads, zip tool output, generate_document output, …).
    """

    name: ClassVar[str] = "send_email"
    version: ClassVar[str] = "1.1.0"
    summary: ClassVar[str] = (
        "Send an email via the configured SMTP server with optional "
        "attachments (requires human approval)."
    )
    permissions: ClassVar[list[str]] = ["email:send"]
    rate_limit_per_minute: ClassVar[int] = 10
    requires_approval: ClassVar[bool] = True
    # No allowlist configured → accept any recipient (approval is the gate).
    restrict_to_user_when_no_allowlist: ClassVar[bool] = False
    available: ClassVar[bool] = True
