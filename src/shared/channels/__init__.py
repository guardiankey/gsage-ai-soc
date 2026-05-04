"""Shared metadata model used by every gSage channel.

A *channel* is an inbound/outbound integration that lets users talk to
the SOC (telegram, teams, email, etc.). Each channel module ships a
``CHANNEL_SPEC`` constant of type :class:`ChannelSpec` so the
documentation generator (``scripts/generate_channels_docs.py``) can
produce operator-facing pages straight from the source.

The spec captures three layers of configuration:

1. **Per-organisation settings** stored in the database
   (``GSageInterfaceProfile.interface_config`` JSONB column, or for
   email the dedicated ``GSageEmailAccount`` table). Described as a
   JSON-Schema fragment in :attr:`ChannelSpec.interface_config_schema`.

2. **Cluster-wide environment variables** consumed by the worker /
   handler at startup time. Described as a list of :class:`EnvVar`
   entries in :attr:`ChannelSpec.env_vars`.

3. **Operational prerequisites** (network egress, third-party app
   registration, webhook URLs, etc.) — free-form strings rendered as
   bullet lists.

The spec deliberately avoids importing the channel's runtime code, so
the generator can introspect every channel without booting the workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class EnvVar:
    """A single cluster-wide environment variable consumed by the channel."""

    name: str
    """Variable name as it appears in ``.env`` (e.g. ``TELEGRAM_RELOAD_INTERVAL``)."""

    description: str
    """One-line operator-facing description."""

    default: Any = None
    """Default value baked into the code (``None`` means *required*)."""

    sensitive: bool = False
    """Hide the value in generated docs / `.env.channels.example`."""

    type: str = "string"
    """Logical type for documentation only: ``string`` | ``integer`` | ``boolean`` | …"""


@dataclass(frozen=True)
class CliExample:
    """A copy-pastable invocation of the channel's ops_cli command."""

    title: str
    command: str  # raw shell snippet (multiline allowed)


@dataclass(frozen=True)
class ChannelSpec:
    """Static description of one channel — single source of truth for docs."""

    interface: str
    """Stable identifier (matches ``GSageInterfaceProfile.interface``)."""

    summary: str
    """One-sentence description."""

    description: str
    """Multi-paragraph free-form description (Markdown allowed)."""

    # ── Configuration surface ────────────────────────────────────────
    config_storage: str
    """Where per-org config is stored. Common values:

    - ``"interface_profile.interface_config"`` — JSONB column on
      ``GSageInterfaceProfile``.
    - ``"email_account_table"`` — dedicated ``GSageEmailAccount`` row.
    """

    interface_config_schema: Optional[dict] = None
    """JSON-Schema fragment describing each field (``properties`` /
    ``required``). Set to ``None`` when the channel uses a dedicated
    table whose columns are documented elsewhere."""

    interface_config_defaults: dict = field(default_factory=dict)
    """Hard-coded defaults paired with :attr:`interface_config_schema`."""

    # ── Runtime configuration ────────────────────────────────────────
    env_vars: list[EnvVar] = field(default_factory=list)
    """Cluster-wide env vars (typically ``Settings`` fields)."""

    # ── Wiring & operations ──────────────────────────────────────────
    cli_module: Optional[str] = None
    """Dotted path to the ops_cli sub-command (e.g. ``src.ops_cli.channels.telegram``)."""

    cli_examples: list[CliExample] = field(default_factory=list)
    """Ready-to-run command examples (rendered as fenced shell blocks)."""

    worker_modules: list[str] = field(default_factory=list)
    """Dotted module paths for the worker / handler runtimes."""

    webhook_paths: list[str] = field(default_factory=list)
    """Inbound HTTP routes (when applicable). May contain placeholders."""

    prerequisites: list[str] = field(default_factory=list)
    """Bullet list of operational prerequisites (network, third-party setup, …)."""

    source_files: list[str] = field(default_factory=list)
    """Workspace-relative paths considered authoritative for this channel.

    The generator scans these files for *loose* env-var reads
    (``os.environ.get`` / ``os.getenv``) and lists them under "Other
    environment variables" — same UX as the tools generator.
    """
