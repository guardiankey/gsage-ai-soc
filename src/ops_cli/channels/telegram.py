"""ops_cli — telegram channel management.

Usage (inside backend_api container)::

    python -m ops_cli channels telegram upsert \
        --org-slug gsage \
        --description "Main SOC bot" \
        --bot-token-stdin [--json]

Creates or updates an ``interface = 'telegram'`` profile (org-wide, no
``dept_id``, no ``user_id``) and stores the bot token inside
``interface_config.bot_token`` via the existing JSONB column.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from sqlalchemy import select

from src.ops_cli._helpers import print_result, resolve_org_id


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)

    upsert = sub.add_parser("upsert", help="Create or update the org-wide telegram profile")
    upsert.add_argument("--org-id", default=None)
    upsert.add_argument("--org-slug", default=None)
    upsert.add_argument("--description", default=None)
    upsert.add_argument("--bot-token", default=None,
                        help="Telegram bot token (prefer --bot-token-stdin)")
    upsert.add_argument("--bot-token-stdin", action="store_true",
                        help="Read the bot token from stdin (first non-empty line)")
    upsert.add_argument("--deactivate", action="store_true",
                        help="Mark the profile inactive (do not wipe token)")
    upsert.add_argument("--json", dest="json_out", action="store_true")
    upsert.set_defaults(_func=_run_upsert)

    show = sub.add_parser("show", help="Show the current telegram profile (redacted)")
    show.add_argument("--org-id", default=None)
    show.add_argument("--org-slug", default=None)
    show.add_argument("--json", dest="json_out", action="store_true")
    show.set_defaults(_func=_run_show)


def _run_upsert(args: argparse.Namespace) -> int:
    return asyncio.run(_upsert_async(args))


def _run_show(args: argparse.Namespace) -> int:
    return asyncio.run(_show_async(args))


def _read_token(args: argparse.Namespace) -> Optional[str]:
    if args.bot_token_stdin:
        raw = sys.stdin.read()
        for line in raw.splitlines():
            line = line.strip()
            if line:
                return line
        return None
    return args.bot_token


async def _upsert_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    token = _read_token(args)
    if not token and not args.deactivate:
        print("ERROR: bot token is required (use --bot-token-stdin)", file=sys.stderr)
        return 2

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)

        # Match the partial unique index: (org_id, dept_id IS NULL,
        # interface, user_id IS NULL)
        result = await db.execute(
            select(GSageInterfaceProfile).where(
                GSageInterfaceProfile.org_id == org_id,
                GSageInterfaceProfile.interface == "telegram",
                GSageInterfaceProfile.dept_id.is_(None),
                GSageInterfaceProfile.user_id.is_(None),
            )
        )
        profile = result.scalar_one_or_none()

        if profile is None:
            profile = GSageInterfaceProfile(
                org_id=org_id,
                interface="telegram",
                mode="denylist",
                tool_permissions=[],
                is_active=not args.deactivate,
                description=args.description,
                interface_config={"bot_token": token} if token else {},
            )
            created = True
        else:
            created = False
            if args.description is not None:
                profile.description = args.description
            cfg = dict(profile.interface_config or {})
            if token:
                cfg["bot_token"] = token
            profile.interface_config = cfg
            profile.is_active = not args.deactivate

        db.add(profile)
        await db.commit()
        await db.refresh(profile)

        print_result(
            {
                "status": "ok",
                "message": f"telegram profile {'created' if created else 'updated'}",
                "details": {
                    "id": str(profile.id),
                    "org_id": str(profile.org_id),
                    "active": profile.is_active,
                    "description": profile.description,
                    "has_token": bool(
                        (profile.interface_config or {}).get("bot_token")
                    ),
                },
            },
            json_out=args.json_out,
        )
    return 0


async def _show_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        result = await db.execute(
            select(GSageInterfaceProfile).where(
                GSageInterfaceProfile.org_id == org_id,
                GSageInterfaceProfile.interface == "telegram",
                GSageInterfaceProfile.dept_id.is_(None),
                GSageInterfaceProfile.user_id.is_(None),
            )
        )
        profile = result.scalar_one_or_none()
        payload = {
            "org_id": str(org_id),
            "exists": profile is not None,
        }
        if profile is not None:
            cfg = dict(profile.interface_config or {})
            if cfg.get("bot_token"):
                cfg["bot_token"] = "••••••"
            payload.update({
                "id": str(profile.id),
                "active": profile.is_active,
                "description": profile.description,
                "mode": profile.mode,
                "interface_config": cfg,
            })

        if args.json_out:
            json.dump(payload, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            for k, v in payload.items():
                print(f"{k}: {v}")
    return 0
