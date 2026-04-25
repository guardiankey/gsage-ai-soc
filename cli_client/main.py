"""gSage AI — CLI client entry point."""

from __future__ import annotations

import logging
import sys

from rich.console import Console
from rich.markup import escape as rich_escape

from cli_client.client import APIError, GSageAPIClient
from cli_client.config import Config
from cli_client.repl import GSageREPL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

# Suppress httpx INFO logs (only show warnings and errors)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def main() -> int:
    """Entry point for the CLI client."""
    console = Console()

    try:
        config = Config.from_env()

        if config.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            logger.debug("Debug mode enabled")
            logger.debug(f"API Host: {config.api_host}")

    except ValueError as exc:
        console.print(f"[bold red]Configuration error: {exc}[/bold red]")
        console.print("\n[yellow]Authentication options:[/yellow]")
        console.print("  # Option A — API key (org ID required):")
        console.print("  export GSAGE_API_KEY='gk_live_...'")
        console.print("  export GSAGE_ORG_ID='<your-org-uuid>'")
        console.print("\n  # Option B — email/password (auto-login at startup):")
        console.print("  export GSAGE_EMAIL='you@example.com'")
        console.print("  export GSAGE_PASSWORD='yourpassword'")
        console.print("\n  # Option C — no env vars; type 'login' inside the REPL")
        return 1

    try:
        with GSageAPIClient(config) as client:
            # Auto-login if email + password provided via env
            if config.email and config.password and not config.api_key:
                try:
                    client.login(email=config.email, password=config.password)
                    console.print(f"[dim]Auto-logged in as {config.email}[/dim]")
                except APIError as exc:
                    console.print(f"[bold red]Auto-login failed: {exc}[/bold red]")
                    console.print("[yellow]You can still login manually inside the REPL.[/yellow]")

            repl = GSageREPL(config, client)
            repl.run()

        return 0

    except APIError as exc:
        console.print(f"[bold red]API Error: {rich_escape(str(exc))}[/bold red]")
        if config.debug:
            logger.exception("API error")
        return 1

    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted by user[/dim]")
        return 130

    except Exception as exc:
        console.print(f"[bold red]Unexpected error: {rich_escape(str(exc))}[/bold red]")
        if config.debug:
            logger.exception("Unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
