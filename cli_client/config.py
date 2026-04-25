"""CLI client configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Configuration for the gSage AI CLI client.

    Authentication priority:
    1. GSAGE_API_KEY — static API key (requires GSAGE_ORG_ID)
    2. GSAGE_EMAIL / GSAGE_PASSWORD — auto-login at startup
    3. (none) — user runs 'login' command interactively
    """

    # API connection settings
    api_host: str

    # Option A: API key authentication (requires org_id to be set).
    # For CLI-optimised responses (terse, terminal-friendly formatting without heavy
    # markdown), create a personal API key with interface="cli" via the web UI or
    # POST /v1/orgs/{org_id}/me/api-keys with body {"name": "...", "interface": "cli"}.
    # Keys without an explicit interface default to "web" for personal keys and "api"
    # for org-level keys.
    api_key: str | None = None

    # Option B: Email/password for auto-login at startup
    email: str | None = None
    password: str | None = None

    # Organization ID — required for org-scoped routes.
    # Automatically populated from JWT claims after login.
    # Must be set via env when using API key auth.
    org_id: str | None = None

    # Department ID — scopes resources to a specific department.
    # Populated after login (defaults to the default dept) or set via
    # GSAGE_DEPT_ID env var or the 'dept set <slug>' command.
    dept_id: str | None = None

    # Optional: conversation ID to resume
    conversation_id: str | None = None

    # Output settings
    debug: bool = False
    output_format: str = "markdown"  # "markdown" or "plain"

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables.

        Auth (at least one option is needed to use org-scoped routes):
            GSAGE_API_KEY    — static API key; GSAGE_ORG_ID also required
            GSAGE_EMAIL      — email for JWT login at startup
            GSAGE_PASSWORD   — password for JWT login at startup

        Other:
            GSAGE_API_HOST           — default: http://localhost:8000
            GSAGE_ORG_ID             — required when using API key auth
            GSAGE_DEPT_ID            — optional department UUID (e.g. default dept)
            GSAGE_CONVERSATION_ID    — resume an existing conversation
            GSAGE_DEBUG              — true/false
            GSAGE_OUTPUT_FORMAT      — markdown/plain
        """
        api_key = os.getenv("GSAGE_API_KEY")
        email = os.getenv("GSAGE_EMAIL")
        password = os.getenv("GSAGE_PASSWORD")
        org_id = os.getenv("GSAGE_ORG_ID")
        dept_id = os.getenv("GSAGE_DEPT_ID")
        api_host = os.getenv("GSAGE_API_HOST", "http://localhost:8000")
        conversation_id = os.getenv("GSAGE_CONVERSATION_ID")
        debug = os.getenv("GSAGE_DEBUG", "").lower() in ("true", "1", "yes")
        output_format = os.getenv("GSAGE_OUTPUT_FORMAT", "markdown")

        if api_key and not org_id:
            raise ValueError(
                "GSAGE_ORG_ID is required when using GSAGE_API_KEY. "
                "Find your org id in the web UI or use email/password login instead."
            )

        return cls(
            api_key=api_key,
            email=email,
            password=password,
            org_id=org_id,
            dept_id=dept_id,
            api_host=api_host.rstrip("/"),
            conversation_id=conversation_id,
            debug=debug,
            output_format=output_format,
        )
