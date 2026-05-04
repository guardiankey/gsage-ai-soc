"""Static metadata for the Email channel — consumed by
``scripts/generate_channels_docs.py``.

Email is special: per-org configuration lives in a dedicated relational
table (:class:`src.shared.models.email_account.GSageEmailAccount`), not
in the ``interface_profile.interface_config`` JSONB column. The schema
below mirrors the *operator-facing* subset of that table — enough to
guide a human (or a JSON-driven UI) when creating an account.
"""

from __future__ import annotations

from src.shared.channels import ChannelSpec, CliExample, EnvVar


CHANNEL_SPEC = ChannelSpec(
    interface="email",
    summary="Email channel — IMAP IDLE listener + SMTP sender backed by the "
            "``GSageEmailAccount`` table.",
    description=(
        "The Email worker runs in the ``email_worker`` container. It "
        "polls / IDLE-watches every active ``GSageEmailAccount`` row and "
        "dispatches incoming messages to MCP after rate limiting and "
        "thread resolution. Outbound replies are sent via SMTP using the "
        "same account credentials. Each organisation can register multiple "
        "mailboxes; messages from unknown senders are moved into the "
        "``email_unknown_sender_folder`` (default ``Unknown-Senders``)."
    ),
    config_storage="email_account_table",
    interface_config_schema={
        "type": "object",
        "required": [
            "display_name", "email",
            "imap_host", "imap_port", "imap_username", "imap_password",
            "smtp_host", "smtp_port",
        ],
        "properties": {
            "display_name": {
                "type": "string",
                "description": "Human-readable label (e.g. ``SOC Mailbox``).",
            },
            "email": {
                "type": "string",
                "description": "From address; also the unique key for the account row.",
            },
            "imap_host": {"type": "string", "description": "IMAP server hostname."},
            "imap_port": {"type": "integer", "default": 993,
                          "description": "IMAP port (993 for IMAPS, 143 for STARTTLS)."},
            "imap_use_tls": {"type": "boolean", "default": True,
                             "description": "Use IMAPS / STARTTLS."},
            "imap_verify_ssl": {"type": "boolean", "default": True,
                                "description": "Verify TLS certificates "
                                               "(set to ``false`` for self-signed)."},
            "imap_username": {"type": "string", "description": "IMAP login."},
            "imap_password": {"type": "string", "sensitive": True,
                              "description": "IMAP password (encrypted at rest)."},
            "imap_folder": {"type": "string", "default": "INBOX",
                            "description": "Mailbox to watch."},
            "smtp_host": {"type": "string", "description": "SMTP server hostname."},
            "smtp_port": {"type": "integer", "default": 587,
                          "description": "SMTP port (587 for STARTTLS, 465 for SMTPS)."},
            "smtp_use_tls": {"type": "boolean", "default": True,
                             "description": "Use STARTTLS / SMTPS."},
            "smtp_verify_ssl": {"type": "boolean", "default": True,
                                "description": "Verify TLS certificates."},
            "smtp_username": {"type": "string", "default": "",
                              "description": "SMTP login. Leave blank for "
                                             "unauthenticated relay."},
            "smtp_password": {"type": "string", "sensitive": True,
                              "description": "SMTP password (encrypted at rest)."},
            "sender_name": {"type": "string",
                            "description": "Display name used in the ``From:`` header."},
            "subject_prefix": {"type": "string",
                               "description": "Optional subject prefix for outbound messages."},
            "max_email_size_bytes": {"type": "integer",
                                     "description": "Reject inbound messages larger than this."},
            "polling_interval_seconds": {"type": "integer",
                                         "description": "Fallback poll cadence when "
                                                        "IMAP IDLE is not available."},
        },
    },
    env_vars=[
        EnvVar(
            name="EMAIL_RATE_LIMIT_ORG_DAILY",
            description="Maximum inbound messages processed per organisation "
                        "per UTC day.",
            default=100,
            type="integer",
        ),
        EnvVar(
            name="EMAIL_RATE_LIMIT_USER_HOURLY",
            description="Maximum new threads opened per user per rolling hour.",
            default=10,
            type="integer",
        ),
        EnvVar(
            name="EMAIL_DELETE_AFTER_PROCESS",
            description="When ``true`` permanently delete messages after "
                        "processing; otherwise they are only marked ``\\Seen``.",
            default=False,
            type="boolean",
        ),
        EnvVar(
            name="EMAIL_UNKNOWN_SENDER_FOLDER",
            description="IMAP folder where messages from unknown senders "
                        "are moved (auto-created on first use).",
            default="Unknown-Senders",
        ),
        EnvVar(
            name="SMTP_FROM_EMAIL",
            description="Fallback From address for outbound SOC notifications "
                        "that are not tied to a specific account.",
            default="noreply@gsage.local",
        ),
        EnvVar(
            name="SMTP_FROM_NAME",
            description="Display name paired with ``SMTP_FROM_EMAIL``.",
            default="gSage AI",
        ),
        EnvVar(
            name="SMTP_DEFAULT_FORMAT",
            description="Default body format for outbound notifications: "
                        "``html`` (auto-converts Markdown) or ``text``.",
            default="html",
        ),
    ],
    cli_module="src.ops_cli.channels.email",
    cli_examples=[
        CliExample(
            title="Create / update an email account (passwords on stdin)",
            command=(
                "printf '%s\\n%s\\n' '<imap-pw>' '<smtp-pw>' | \\\n"
                "    python -m ops_cli channels email create \\\n"
                "        --org-slug gsage \\\n"
                "        --display-name \"SOC Mailbox\" \\\n"
                "        --email soc@example.com \\\n"
                "        --imap-host mail.example.com --imap-port 993 --imap-user soc \\\n"
                "        --smtp-host mail.example.com --smtp-port 587 --smtp-user soc \\\n"
                "        --imap-password-stdin --smtp-password-stdin \\\n"
                "        --test"
            ),
        ),
        CliExample(
            title="List accounts in an org",
            command="python -m ops_cli channels email list --org-slug gsage",
        ),
    ],
    worker_modules=["src.email_worker.main"],
    webhook_paths=[],  # No inbound HTTP — IMAP only.
    prerequisites=[
        "An IMAP+SMTP-capable mailbox the worker can authenticate to.",
        "Network egress from `email_worker` to the IMAP and SMTP servers.",
        "The `email_worker` service must be running "
        "(`docker compose up email_worker`).",
        "Mailboxes are encrypted at rest using the application secret — "
        "rotating that secret invalidates stored credentials.",
    ],
    source_files=[
        "src/email_worker/main.py",
        "src/email_worker/imap_client.py",
        "src/email_worker/smtp_sender.py",
        "src/email_worker/parser.py",
        "src/email_worker/rate_limiter.py",
        "src/email_worker/resolver.py",
        "src/email_worker/thread_manager.py",
        "src/shared/models/email_account.py",
        "src/ops_cli/channels/email.py",
    ],
)
