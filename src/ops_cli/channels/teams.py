"""ops_cli — microsoft teams channel management.

Usage (inside backend_api container)::

    python -m ops_cli channels teams upsert \\
        --org-slug gsage \\
        --description "Main SOC bot" \\
        --app-id 00000000-0000-0000-0000-000000000000 \\
        --tenant-id 00000000-0000-0000-0000-000000000000 \\
        --app-password-stdin [--json]

Creates or updates an ``interface = 'teams'`` profile (org-wide, no
``dept_id``, no ``user_id``) and stores the Azure Bot credentials inside
``interface_config`` (``app_id``, ``app_password``, ``tenant_id``).

After upsert, register the webhook in the Azure Bot resource:

    Messaging endpoint:
        https://<your-host>/api/v1/channels/teams/<profile_id>/messages
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

    upsert = sub.add_parser("upsert", help="Create or update the org-wide teams profile")
    upsert.add_argument("--org-id", default=None)
    upsert.add_argument("--org-slug", default=None)
    upsert.add_argument("--description", default=None)
    upsert.add_argument("--app-id", default=None,
                        help="Azure App Registration (client) ID")
    upsert.add_argument("--tenant-id", default=None,
                        help="Azure tenant ID (used for Microsoft Graph lookups)")
    upsert.add_argument("--app-password", default=None,
                        help="Azure App client secret (prefer --app-password-stdin)")
    upsert.add_argument("--app-password-stdin", action="store_true",
                        help="Read the app password (client secret) from stdin")
    upsert.add_argument("--deactivate", action="store_true",
                        help="Mark the profile inactive (do not wipe credentials)")
    upsert.add_argument("--json", dest="json_out", action="store_true")
    upsert.set_defaults(_func=_run_upsert)

    show = sub.add_parser("show", help="Show the current teams profile (redacted)")
    show.add_argument("--org-id", default=None)
    show.add_argument("--org-slug", default=None)
    show.add_argument("--json", dest="json_out", action="store_true")
    show.set_defaults(_func=_run_show)

    delete = sub.add_parser("delete", help="Delete the org-wide teams profile")
    delete.add_argument("--org-id", default=None)
    delete.add_argument("--org-slug", default=None)
    delete.add_argument("--yes", action="store_true",
                       help="Skip the interactive confirmation prompt")
    delete.add_argument("--json", dest="json_out", action="store_true")
    delete.set_defaults(_func=_run_delete)


def _run_upsert(args: argparse.Namespace) -> int:
    return asyncio.run(_upsert_async(args))


def _run_show(args: argparse.Namespace) -> int:
    return asyncio.run(_show_async(args))


def _run_delete(args: argparse.Namespace) -> int:
    return asyncio.run(_delete_async(args))


def _read_password(args: argparse.Namespace) -> Optional[str]:
    if args.app_password_stdin:
        raw = sys.stdin.read()
        for line in raw.splitlines():
            line = line.strip()
            if line:
                return line
        return None
    return args.app_password


async def _upsert_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    password = _read_password(args)
    if not args.deactivate and not (args.app_id or password or args.tenant_id):
        print(
            "ERROR: at least one of --app-id, --tenant-id or "
            "--app-password/--app-password-stdin is required",
            file=sys.stderr,
        )
        return 2

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)

        result = await db.execute(
            select(GSageInterfaceProfile).where(
                GSageInterfaceProfile.org_id == org_id,
                GSageInterfaceProfile.interface == "teams",
                GSageInterfaceProfile.dept_id.is_(None),
                GSageInterfaceProfile.user_id.is_(None),
            )
        )
        profile = result.scalar_one_or_none()

        new_cfg: dict = {}
        if args.app_id:
            new_cfg["app_id"] = args.app_id
        if password:
            new_cfg["app_password"] = password
        if args.tenant_id:
            new_cfg["tenant_id"] = args.tenant_id

        if profile is None:
            profile = GSageInterfaceProfile(
                org_id=org_id,
                interface="teams",
                mode="denylist",
                tool_permissions=[],
                is_active=not args.deactivate,
                description=args.description,
                interface_config=new_cfg or {},
            )
            created = True
        else:
            created = False
            if args.description is not None:
                profile.description = args.description
            cfg = dict(profile.interface_config or {})
            cfg.update(new_cfg)
            profile.interface_config = cfg
            profile.is_active = not args.deactivate
            # Keep an open denylist if no explicit tool permissions —
            # consistent with telegram upsert behaviour.
            if not (profile.tool_permissions or []):
                profile.mode = "denylist"
                profile.tool_permissions = []

        db.add(profile)
        await db.commit()
        await db.refresh(profile)

        cfg = profile.interface_config or {}
        print_result(
            {
                "status": "ok",
                "message": f"teams profile {'created' if created else 'updated'}",
                "details": {
                    "id": str(profile.id),
                    "org_id": str(profile.org_id),
                    "active": profile.is_active,
                    "description": profile.description,
                    "has_app_id": bool(cfg.get("app_id")),
                    "has_app_password": bool(cfg.get("app_password")),
                    "has_tenant_id": bool(cfg.get("tenant_id")),
                    "webhook_path": (
                        f"/api/v1/channels/teams/{profile.id}/messages"
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
                GSageInterfaceProfile.interface == "teams",
                GSageInterfaceProfile.dept_id.is_(None),
                GSageInterfaceProfile.user_id.is_(None),
            )
        )
        profile = result.scalar_one_or_none()
        payload: dict = {
            "org_id": str(org_id),
            "exists": profile is not None,
        }
        if profile is not None:
            cfg = dict(profile.interface_config or {})
            if cfg.get("app_password"):
                cfg["app_password"] = "••••••"
            payload.update({
                "id": str(profile.id),
                "active": profile.is_active,
                "description": profile.description,
                "mode": profile.mode,
                "interface_config": cfg,
                "webhook_path": f"/api/v1/channels/teams/{profile.id}/messages",
            })

        if args.json_out:
            json.dump(payload, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            for k, v in payload.items():
                print(f"{k}: {v}")
    return 0


async def _delete_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        result = await db.execute(
            select(GSageInterfaceProfile).where(
                GSageInterfaceProfile.org_id == org_id,
                GSageInterfaceProfile.interface == "teams",
                GSageInterfaceProfile.dept_id.is_(None),
                GSageInterfaceProfile.user_id.is_(None),
            )
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            print_result(
                {"status": "ok", "message": "no teams profile to delete",
                 "details": {"org_id": str(org_id)}},
                json_out=args.json_out,
            )
            return 0

        if not args.yes:
            sys.stderr.write(
                f"About to delete teams profile {profile.id} for org "
                f"{org_id}. Re-run with --yes to confirm.\n"
            )
            return 1

        await db.delete(profile)
        await db.commit()
        print_result(
            {"status": "ok", "message": "teams profile deleted",
             "details": {"id": str(profile.id), "org_id": str(org_id)}},
            json_out=args.json_out,
        )
    return 0
