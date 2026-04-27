"""ops_cli — email channel management.

Usage (inside backend_api container)::

    python -m ops_cli channels email create \
        --org-slug gsage \
        --display-name "SOC Mailbox" \
        --email soc@example.com \
        --imap-host mail.example.com --imap-port 993 --imap-user soc \
        --smtp-host mail.example.com --smtp-port 587 --smtp-user soc \
        --imap-password-stdin --smtp-password-stdin \
        [--test] [--json]

The caller wrapper (``configure-email-channel.sh``) feeds the two passwords on
stdin (newline-separated) and sets the ``--*-password-stdin`` flags so that
secrets never appear in argv or logs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any, Optional

from sqlalchemy import select

from src.ops_cli._helpers import print_result, resolve_org_id


# ── argparse wiring ────────────────────────────────────────────────────────

def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)

    create = sub.add_parser("create", help="Create or update an email account")
    _add_common_args(create)
    create.add_argument("--display-name", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--imap-host", required=True)
    create.add_argument("--imap-port", type=int, default=993)
    create.add_argument("--imap-user", required=True)
    create.add_argument("--imap-no-tls", action="store_true")
    create.add_argument(
        "--imap-no-verify-ssl",
        action="store_true",
        help="Skip TLS certificate verification on IMAP (self-signed certs).",
    )
    create.add_argument("--smtp-host", required=True)
    create.add_argument("--smtp-port", type=int, default=587)
    # SMTP user is optional: blank == unauthenticated relay (some on-prem MTAs).
    create.add_argument("--smtp-user", default="")
    create.add_argument("--smtp-no-tls", action="store_true")
    create.add_argument(
        "--smtp-no-verify-ssl",
        action="store_true",
        help="Skip TLS certificate verification on SMTP (self-signed certs).",
    )
    create.add_argument("--imap-password", default=None,
                        help="IMAP password (prefer --imap-password-stdin)")
    create.add_argument("--smtp-password", default=None,
                        help="SMTP password (prefer --smtp-password-stdin)")
    create.add_argument("--imap-password-stdin", action="store_true",
                        help="Read IMAP password from stdin (first line)")
    create.add_argument("--smtp-password-stdin", action="store_true",
                        help="Read SMTP password from stdin (second line, or "
                             "same line if --imap-password also provided)")
    create.add_argument("--test", action="store_true",
                        help="Probe IMAP LOGIN + SMTP AUTH before persisting")
    create.set_defaults(_func=_run_create)

    list_p = sub.add_parser("list", help="List email accounts in an org")
    _add_common_args(list_p)
    list_p.set_defaults(_func=_run_list)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--org-id", default=None)
    p.add_argument("--org-slug", default=None)
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Emit machine-readable JSON output")


# ── Commands ───────────────────────────────────────────────────────────────

def _run_create(args: argparse.Namespace) -> int:
    return asyncio.run(_create_async(args))


def _run_list(args: argparse.Namespace) -> int:
    return asyncio.run(_list_async(args))


async def _create_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.email_account import GSageEmailAccount  # noqa: PLC0415

    imap_pw, smtp_pw = _read_passwords(args)
    if not imap_pw:
        print("ERROR: IMAP password is required", file=sys.stderr)
        return 2
    # SMTP password is optional (unauthenticated relay).

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)

        # Upsert by unique ``email`` column.
        result = await db.execute(
            select(GSageEmailAccount).where(GSageEmailAccount.email == args.email)
        )
        acc: Optional[GSageEmailAccount] = result.scalar_one_or_none()

        if acc is None:
            acc = GSageEmailAccount(
                org_id=org_id,
                display_name=args.display_name,
                email=args.email,
                is_active=True,
                imap_host=args.imap_host,
                imap_port=args.imap_port,
                imap_use_tls=not args.imap_no_tls,
                imap_verify_ssl=not args.imap_no_verify_ssl,
                imap_username=args.imap_user,
                smtp_host=args.smtp_host,
                smtp_port=args.smtp_port,
                smtp_use_tls=not args.smtp_no_tls,
                smtp_verify_ssl=not args.smtp_no_verify_ssl,
                smtp_username=args.smtp_user,
                sender_name=args.display_name,
            )
            created = True
        else:
            acc.org_id = org_id
            acc.display_name = args.display_name
            acc.is_active = True
            acc.imap_host = args.imap_host
            acc.imap_port = args.imap_port
            acc.imap_use_tls = not args.imap_no_tls
            acc.imap_verify_ssl = not args.imap_no_verify_ssl
            acc.imap_username = args.imap_user
            acc.smtp_host = args.smtp_host
            acc.smtp_port = args.smtp_port
            acc.smtp_use_tls = not args.smtp_no_tls
            acc.smtp_verify_ssl = not args.smtp_no_verify_ssl
            acc.smtp_username = args.smtp_user
            created = False

        # Encrypt passwords via property setters on the model.
        acc.imap_password = imap_pw  # type: ignore[assignment]
        if smtp_pw:
            acc.smtp_password = smtp_pw  # type: ignore[assignment]

        if args.test:
            ok, err = await _probe_connectivity(acc, imap_pw, smtp_pw)
            if not ok:
                print_result(
                    {
                        "status": "error",
                        "message": f"connectivity probe failed: {err}",
                    },
                    json_out=args.json_out,
                )
                return 3

        db.add(acc)
        await db.commit()
        await db.refresh(acc)

        print_result(
            {
                "status": "ok",
                "message": f"email account {'created' if created else 'updated'}",
                "details": {
                    "id": str(acc.id),
                    "org_id": str(acc.org_id),
                    "email": acc.email,
                    "imap": f"{acc.imap_host}:{acc.imap_port}",
                    "smtp": f"{acc.smtp_host}:{acc.smtp_port}",
                    "active": acc.is_active,
                },
            },
            json_out=args.json_out,
        )
    return 0


async def _list_async(args: argparse.Namespace) -> int:
    from src.shared.database import _get_session_maker  # noqa: PLC0415
    from src.shared.models.email_account import GSageEmailAccount  # noqa: PLC0415

    session_maker = _get_session_maker()
    async with session_maker() as db:
        org_id = await resolve_org_id(db, org_id=args.org_id, org_slug=args.org_slug)
        result = await db.execute(
            select(GSageEmailAccount)
            .where(GSageEmailAccount.org_id == org_id)
            .order_by(GSageEmailAccount.email)
        )
        accounts: list[dict[str, Any]] = [
            {
                "id": str(a.id),
                "email": a.email,
                "display_name": a.display_name,
                "imap": f"{a.imap_host}:{a.imap_port}",
                "smtp": f"{a.smtp_host}:{a.smtp_port}",
                "active": a.is_active,
            }
            for a in result.scalars().all()
        ]
        if args.json_out:
            import json as _json  # noqa: PLC0415
            _json.dump({"org_id": str(org_id), "accounts": accounts},
                       sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            print(f"Email accounts in org {org_id}:")
            if not accounts:
                print("  (none)")
            for a in accounts:
                flag = "✓" if a["active"] else "·"
                print(f"  {flag} {a['email']}  imap={a['imap']} smtp={a['smtp']}")
    return 0


# ── Helpers ────────────────────────────────────────────────────────────────

def _read_passwords(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """Resolve IMAP/SMTP passwords from args or stdin.

    Wire format when both are on stdin:
        line 1 → IMAP password
        line 2 → SMTP password  (omit line for "no SMTP auth")
    """
    imap_pw = args.imap_password
    smtp_pw = args.smtp_password

    if args.imap_password_stdin or args.smtp_password_stdin:
        raw = sys.stdin.read()
        lines = raw.splitlines()
        if args.imap_password_stdin:
            imap_pw = lines[0] if lines else None
        if args.smtp_password_stdin:
            idx = 1 if args.imap_password_stdin else 0
            smtp_pw = lines[idx] if len(lines) > idx else None

    return imap_pw, (smtp_pw or None)


async def _probe_connectivity(
    acc: Any,
    imap_pw: str,
    smtp_pw: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Run a quick IMAP LOGIN + SMTP AUTH probe.

    Best-effort: any exception on either side becomes an error string.
    """
    # IMAP probe — use stdlib imaplib in a thread to avoid pulling new deps.
    import asyncio as _aio  # noqa: PLC0415
    import imaplib  # noqa: PLC0415
    import smtplib  # noqa: PLC0415
    import ssl  # noqa: PLC0415

    def _imap_login() -> Optional[str]:
        try:
            if acc.imap_use_tls:
                cli = imaplib.IMAP4_SSL(acc.imap_host, acc.imap_port, timeout=15)
            else:
                cli = imaplib.IMAP4(acc.imap_host, acc.imap_port, timeout=15)
            try:
                cli.login(acc.imap_username, imap_pw)
                cli.logout()
            except Exception as exc:
                return f"IMAP login: {exc}"
        except Exception as exc:
            return f"IMAP connect: {exc}"
        return None

    def _smtp_auth() -> Optional[str]:
        if not smtp_pw:
            return None  # unauthenticated relay — skip probe
        try:
            if acc.smtp_use_tls:
                cli = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=15)
                cli.ehlo()
                cli.starttls(context=ssl.create_default_context())
                cli.ehlo()
            else:
                cli = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=15)
                cli.ehlo()
            try:
                cli.login(acc.smtp_username, smtp_pw)
                cli.quit()
            except Exception as exc:
                return f"SMTP login: {exc}"
        except Exception as exc:
            return f"SMTP connect: {exc}"
        return None

    err = await _aio.to_thread(_imap_login)
    if err:
        return False, err
    err = await _aio.to_thread(_smtp_auth)
    if err:
        return False, err
    return True, None
