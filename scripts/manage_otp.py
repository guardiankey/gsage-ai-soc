#!/usr/bin/env python3
"""Admin utility for managing OTP (TOTP 2FA) user settings.

Connects directly to the database — requires the application environment
variables to be set (DATABASE_URL, ENCRYPTION_KEY, etc.).

Usage::

    # List all users and their OTP status (optionally filter by org slug)
    python scripts/manage_otp.py list
    python scripts/manage_otp.py list --org my-org

    # Disable 2FA for a user (keeps secret/backup codes, only clears otp_enabled)
    python scripts/manage_otp.py disable user@example.com

    # Fully reset 2FA for a user (clears secret, backup codes, trusted devices)
    python scripts/manage_otp.py reset user@example.com

    # Delete all trusted devices for a user
    python scripts/manage_otp.py clear-devices user@example.com
"""

from __future__ import annotations

import asyncio
import argparse
import os
import sys
from typing import Optional

# Ensure repo root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from src.shared.database import _get_session_maker
from src.shared.models.user import GSageUser
from src.shared.models.trusted_device import GSageTrustedDevice
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.models.organization import GSageOrganization


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_user_by_email(session, email: str) -> Optional[GSageUser]:
    result = await session.execute(
        select(GSageUser)
        .where(GSageUser.email == email.lower().strip())
        .options(selectinload(GSageUser.trusted_devices))
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

async def cmd_list(org_slug: Optional[str] = None) -> None:
    """List users and their OTP status."""
    from rich.table import Table
    from rich.console import Console

    console = Console()

    async with _get_session_maker()() as session:
        query = (
            select(GSageUser, GSageUserOrganization, GSageOrganization)
            .join(
                GSageUserOrganization,
                GSageUserOrganization.user_id == GSageUser.id,
                isouter=True,
            )
            .join(
                GSageOrganization,
                GSageOrganization.id == GSageUserOrganization.org_id,
                isouter=True,
            )
        )

        if org_slug:
            query = query.where(GSageOrganization.slug == org_slug)

        query = query.order_by(GSageUser.email)
        rows = (await session.execute(query)).all()

        table = Table(title="OTP Status", show_lines=True)
        table.add_column("Email", style="cyan", no_wrap=True)
        table.add_column("Name")
        table.add_column("Org")
        table.add_column("OTP", justify="center")
        table.add_column("Confirmed At")
        table.add_column("Trusted Devices", justify="right")

        # Collect device counts
        device_counts: dict[str, int] = {}
        for user, _mem, _org in rows:
            if str(user.id) not in device_counts:
                count_result = await session.execute(
                    select(GSageTrustedDevice)
                    .where(GSageTrustedDevice.user_id == user.id)
                )
                device_counts[str(user.id)] = len(count_result.scalars().all())

        seen_users: set[str] = set()
        for user, _mem, org in rows:
            uid = str(user.id)
            if uid in seen_users:
                continue
            seen_users.add(uid)

            otp_badge = "[green]✓ enabled[/green]" if user.otp_enabled else "[red]disabled[/red]"
            confirmed = (
                user.otp_confirmed_at.strftime("%Y-%m-%d %H:%M UTC")
                if user.otp_confirmed_at
                else "-"
            )
            table.add_row(
                user.email,
                user.full_name,
                org.slug if org else "-",
                otp_badge,
                confirmed,
                str(device_counts.get(uid, 0)),
            )

        console.print(table)


async def cmd_disable(email: str) -> None:
    """Disable OTP for a user without clearing the secret (soft disable)."""
    async with _get_session_maker()() as session:
        user = await _get_user_by_email(session, email)
        if user is None:
            print(f"ERROR: User '{email}' not found.", file=sys.stderr)
            sys.exit(1)

        if not user.otp_enabled:
            print(f"INFO: OTP already disabled for {email}.")
            return

        user.otp_enabled = False
        user.otp_confirmed_at = None
        await session.commit()
        print(f"OK: OTP disabled for {email}.")


async def cmd_reset(email: str) -> None:
    """Fully reset OTP for a user — clears secret, backup codes, and trusted devices."""
    async with _get_session_maker()() as session:
        user = await _get_user_by_email(session, email)
        if user is None:
            print(f"ERROR: User '{email}' not found.", file=sys.stderr)
            sys.exit(1)

        user.otp_enabled = False
        user.otp_confirmed_at = None
        user._otp_secret_encrypted = None
        user._otp_backup_codes_encrypted = None

        # Delete trusted devices
        await session.execute(
            delete(GSageTrustedDevice).where(GSageTrustedDevice.user_id == user.id)
        )

        await session.commit()
        print(f"OK: OTP fully reset for {email} (secret, backup codes, and trusted devices cleared).")


async def cmd_clear_devices(email: str) -> None:
    """Delete all trusted devices for a user."""
    async with _get_session_maker()() as session:
        user = await _get_user_by_email(session, email)
        if user is None:
            print(f"ERROR: User '{email}' not found.", file=sys.stderr)
            sys.exit(1)

        result = await session.execute(
            delete(GSageTrustedDevice).where(GSageTrustedDevice.user_id == user.id)
        )
        deleted = result.rowcount
        await session.commit()
        print(f"OK: {deleted} trusted device(s) removed for {email}.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Admin utility for managing OTP/2FA user settings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List users and OTP status")
    p_list.add_argument("--org", metavar="SLUG", help="Filter by organization slug")

    # disable
    p_disable = sub.add_parser("disable", help="Disable OTP for a user (soft disable)")
    p_disable.add_argument("email", help="User email address")

    # reset
    p_reset = sub.add_parser("reset", help="Fully reset OTP for a user")
    p_reset.add_argument("email", help="User email address")

    # clear-devices
    p_clear = sub.add_parser("clear-devices", help="Delete all trusted devices for a user")
    p_clear.add_argument("email", help="User email address")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        asyncio.run(cmd_list(org_slug=getattr(args, "org", None)))
    elif args.command == "disable":
        asyncio.run(cmd_disable(args.email))
    elif args.command == "reset":
        asyncio.run(cmd_reset(args.email))
    elif args.command == "clear-devices":
        asyncio.run(cmd_clear_devices(args.email))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
