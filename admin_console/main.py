"""Entry-point for gSage Admin Console."""

from __future__ import annotations

import argparse
import warnings

# Silence ResourceWarnings emitted during interpreter shutdown by third-party
# libraries (asyncpg, weaviate, elasticsearch) whose __del__ finalizers run
# after the Textual event loop is gone. The Textual app already disposes the
# DB pool and Weaviate client cleanly via on_unmount/action_quit; remaining
# warnings come from sync clients (e.g. Elasticsearch) which we don't control.
warnings.filterwarnings("ignore", category=ResourceWarning)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gsage-admin",
        description="gSage Admin Console — Textual TUI",
    )
    parser.add_argument(
        "--env",
        metavar="FILE",
        default=None,
        help="Path to .env file (defaults to project root .env)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Write errors to ~/.gsage_ai/admin_debug.log",
    )
    args = parser.parse_args()

    from admin_console.config import configure_env  # noqa: PLC0415

    configure_env(args.env)

    from admin_console.app import AdminApp  # noqa: PLC0415

    AdminApp(debug=args.debug).run()


if __name__ == "__main__":
    main()
