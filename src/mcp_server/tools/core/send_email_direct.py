"""gSage AI — Direct Send Email tool (no approval)."""

from __future__ import annotations

from typing import ClassVar

from src.mcp_server.tools.core._send_email_base import _SendEmailBase


class SendEmailDirectTool(_SendEmailBase):
    """MCP tool: send an email immediately, without human approval.

    This variant is meant for narrow, pre-authorised use cases — for
    example, letting the agent send itself (or its owner) notifications
    and reports.  It enforces strict recipient restrictions:

    * If :attr:`allowed_recipients_regex` is configured, only addresses
      matching at least one pattern are accepted.
    * The current user's own addresses (primary + secondary) are always
      accepted.
    * If no allowlist is configured, delivery is restricted to the
      current user's own addresses (safe fallback).

    Attachments may reference any ``file_id`` accessible to the user
    (conversation uploads, zip tool output, generate_document output, …).

    Administrators should grant the dedicated ``email:send_direct``
    permission sparingly.
    """

    name: ClassVar[str] = "send_email_direct"
    config_namespace: ClassVar[str] = "smtp_send"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Send an email immediately, without human approval.  Restricted "
        "to the allowlist regex and/or the current user's own addresses."
    )
    permissions: ClassVar[list[str]] = ["email:send_direct"]
    rate_limit_per_minute: ClassVar[int] = 5
    requires_approval: ClassVar[bool] = False
    # Empty allowlist → deliver only to the current user's own addresses.
    restrict_to_user_when_no_allowlist: ClassVar[bool] = True
    available: ClassVar[bool] = True
