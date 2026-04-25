"""gSage AI — ops_cli top-level argparse dispatcher.

Keeps dependencies minimal — only argparse + the already-installed
application deps. No extra packages required in the runtime image.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from src.ops_cli.channels import email as email_cmd
from src.ops_cli.channels import telegram as telegram_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ops_cli",
        description="gSage AI operator CLI (channel helpers + admin ops).",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # ── channels ──────────────────────────────────────────────
    ch = sub.add_parser("channels", help="Configure external channels (email, telegram)")
    ch_sub = ch.add_subparsers(dest="channel", required=True)

    email_parser = ch_sub.add_parser("email", help="Manage email accounts")
    email_cmd.register(email_parser)

    telegram_parser = ch_sub.add_parser("telegram", help="Manage telegram interface profiles")
    telegram_cmd.register(telegram_parser)

    return parser


def app(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "_func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    return int(func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(app())
