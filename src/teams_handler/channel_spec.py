"""Static metadata for the Microsoft Teams channel — consumed by
``scripts/generate_channels_docs.py``.
"""

from __future__ import annotations

from src.shared.channels import ChannelSpec, CliExample, EnvVar


CHANNEL_SPEC = ChannelSpec(
    interface="teams",
    summary="Microsoft Teams channel — webhook handler bridging Azure Bot "
            "Service activities to MCP-driven conversations.",
    description=(
        "The Teams handler is exposed by the ``backend_api`` service at "
        "``POST /api/v1/channels/teams/{profile_id}/messages``. Each "
        "organisation registers an Azure App + Bot resource and stores "
        "``app_id`` / ``app_password`` / ``tenant_id`` in "
        "``interface_config``. Inbound activities are JWT-validated against "
        "the Microsoft Bot Framework OpenID metadata before being dispatched."
    ),
    config_storage="interface_profile.interface_config",
    interface_config_schema={
        "type": "object",
        "required": ["app_id", "app_password", "tenant_id"],
        "properties": {
            "app_id": {
                "type": "string",
                "description": "Azure App Registration (client) ID.",
            },
            "app_password": {
                "type": "string",
                "sensitive": True,
                "description": "Azure App client secret.",
            },
            "tenant_id": {
                "type": "string",
                "description": "Azure tenant ID (used for Microsoft Graph "
                               "lookups when resolving AAD users to emails).",
            },
        },
    },
    env_vars=[
        EnvVar(
            name="TEAMS_RATE_LIMIT_ORG_DAILY",
            description="Maximum activities forwarded per organisation per UTC day.",
            default=200,
            type="integer",
        ),
        EnvVar(
            name="TEAMS_RATE_LIMIT_USER_HOURLY",
            description="Maximum activities forwarded per Teams user per "
                        "rolling hour.",
            default=30,
            type="integer",
        ),
        EnvVar(
            name="TEAMS_MAX_MESSAGE_LENGTH",
            description="Outbound chunk size before splitting (Teams hard "
                        "cap on a single text activity is 28 KB).",
            default=25_000,
            type="integer",
        ),
        EnvVar(
            name="TEAMS_BOT_OPENID_METADATA_URL",
            description="Microsoft Bot Framework public OpenID configuration "
                        "URL — override only for sovereign / gov clouds.",
            default="https://login.botframework.com/v1/.well-known/openidconfiguration",
        ),
        EnvVar(
            name="TEAMS_GRAPH_EMAIL_CACHE_TTL",
            description="Cache TTL (s) for AAD-Object-ID → email lookups via "
                        "Microsoft Graph (first-contact resolution only).",
            default=86_400,
            type="integer",
        ),
    ],
    cli_module="src.ops_cli.channels.teams",
    cli_examples=[
        CliExample(
            title="Create / update the org-wide Teams profile",
            command=(
                "echo '<client-secret>' | python -m ops_cli channels teams upsert \\\n"
                "    --org-slug gsage \\\n"
                "    --description \"Main SOC bot\" \\\n"
                "    --app-id 00000000-0000-0000-0000-000000000000 \\\n"
                "    --tenant-id 00000000-0000-0000-0000-000000000000 \\\n"
                "    --app-password-stdin"
            ),
        ),
        CliExample(
            title="Inspect the current profile (secret redacted)",
            command="python -m ops_cli channels teams show --org-slug gsage",
        ),
        CliExample(
            title="Delete the profile",
            command="python -m ops_cli channels teams delete --org-slug gsage --yes",
        ),
    ],
    worker_modules=[
        "src.backend_api.app.api.v1.channels_teams",
        "src.teams_handler.handler",
    ],
    webhook_paths=[
        "POST /api/v1/channels/teams/{profile_id}/messages",
    ],
    prerequisites=[
        "An Azure AD App Registration with a client secret.",
        "An Azure Bot resource whose **messaging endpoint** points at "
        "`https://<your-host>/api/v1/channels/teams/<profile_id>/messages`.",
        "The `Microsoft Teams` channel enabled on the Azure Bot resource.",
        "Microsoft Graph delegated permission `User.Read.All` granted to "
        "the App Registration (used for AAD-Object-ID → email lookups).",
        "The `backend_api` service must be reachable from the Bot Framework.",
    ],
    source_files=[
        "src/teams_handler/handler.py",
        "src/teams_handler/conversation_manager.py",
        "src/teams_handler/formatting.py",
        "src/teams_handler/graph_client.py",
        "src/teams_handler/rate_limiter.py",
        "src/teams_handler/resolver.py",
        "src/backend_api/app/api/v1/channels_teams.py",
        "src/ops_cli/channels/teams.py",
    ],
)
