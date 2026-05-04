"""Static metadata for the Telegram channel — consumed by
``scripts/generate_channels_docs.py``.
"""

from __future__ import annotations

from src.shared.channels import ChannelSpec, CliExample, EnvVar


CHANNEL_SPEC = ChannelSpec(
    interface="telegram",
    summary="Telegram bot channel — long-poll worker delivering MCP-driven "
            "conversations to a per-org BotFather bot.",
    description=(
        "The Telegram worker runs in the ``telegram_worker`` container. It "
        "polls every active ``GSageInterfaceProfile`` with ``interface = "
        "'telegram'`` and starts a long-poll loop for each bot using the "
        "token stored in ``interface_config.bot_token``. New bots are "
        "picked up after at most ``TELEGRAM_RELOAD_INTERVAL`` seconds."
    ),
    config_storage="interface_profile.interface_config",
    interface_config_schema={
        "type": "object",
        "required": ["bot_token"],
        "properties": {
            "bot_token": {
                "type": "string",
                "sensitive": True,
                "description": "BotFather token (e.g. ``123456789:ABC-DEF…``).",
            },
        },
    },
    env_vars=[
        EnvVar(
            name="TELEGRAM_RELOAD_INTERVAL",
            description="Interval (s) between DB scans to discover new / "
                        "deactivated bot profiles. ``0`` disables hot-reload.",
            default=300,
            type="integer",
        ),
        EnvVar(
            name="TELEGRAM_RATE_LIMIT_ORG_DAILY",
            description="Maximum messages forwarded per organisation per UTC day.",
            default=200,
            type="integer",
        ),
        EnvVar(
            name="TELEGRAM_RATE_LIMIT_USER_HOURLY",
            description="Maximum messages forwarded per Telegram user per "
                        "rolling hour.",
            default=30,
            type="integer",
        ),
        EnvVar(
            name="TELEGRAM_MAX_MESSAGE_LENGTH",
            description="Outbound chunk size before splitting (Telegram hard "
                        "cap is 4096 characters).",
            default=4096,
            type="integer",
        ),
    ],
    cli_module="src.ops_cli.channels.telegram",
    cli_examples=[
        CliExample(
            title="Create / update the org-wide Telegram profile",
            command=(
                "echo '<bot-token>' | python -m ops_cli channels telegram upsert \\\n"
                "    --org-slug gsage \\\n"
                "    --description \"Main SOC bot\" \\\n"
                "    --bot-token-stdin"
            ),
        ),
        CliExample(
            title="Inspect the current profile (token redacted)",
            command="python -m ops_cli channels telegram show --org-slug gsage",
        ),
    ],
    worker_modules=["src.telegram_worker.main"],
    webhook_paths=[],  # Long-poll worker — no inbound HTTP webhook.
    prerequisites=[
        "A Telegram bot token issued by `@BotFather`.",
        "Network egress from the `telegram_worker` container to "
        "`api.telegram.org` (HTTPS).",
        "The `telegram_worker` service must be running "
        "(`docker compose up telegram_worker`).",
    ],
    source_files=[
        "src/telegram_worker/main.py",
        "src/telegram_worker/handler.py",
        "src/telegram_worker/conversation_manager.py",
        "src/telegram_worker/formatting.py",
        "src/telegram_worker/rate_limiter.py",
        "src/telegram_worker/resolver.py",
        "src/ops_cli/channels/telegram.py",
    ],
)
