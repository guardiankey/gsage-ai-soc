"""Command handlers for the CLI REPL."""

from __future__ import annotations

import getpass
import logging
from typing import TYPE_CHECKING, cast

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from cli_client.pagination import render_paginated_table

if TYPE_CHECKING:
    from cli_client.client import GSageAPIClient
    from cli_client.repl import REPLState

logger = logging.getLogger(__name__)

# Tango color scheme
COLOR_PROMPT = "bright_cyan"
COLOR_USER = "bright_blue"
COLOR_ASSISTANT = "bright_green"
COLOR_ERROR = "bright_red"
COLOR_INFO = "bright_yellow"
COLOR_SUCCESS = "bright_green"

# Required permission per top-level command.  Commands absent from this map
# are always available (no specific permission check).
CMD_PERMISSIONS: dict[str, str] = {
    "conversation": "sessions:read",
    "messages": "sessions:read",
    "approvals": "approvals:read",
    "files": "files:upload",
    "knowledge": "knowledge:read",
    "tasks": "agents:run",
    "api-keys": "apikeys:personal",
    "agents": "scheduled_jobs:read",
    "approval-rules": "approval_rules:read",
    "datastores": "datastores:read",
    "admin": "admin:access",
}


def _c(syntax: str, cmd_key: str | None, rest: str, perms_set: set[str] | None) -> str:
    """Format one help command line with access-aware coloring.

    Args:
        syntax:   The command name / syntax displayed inside brackets.
        cmd_key:  Top-level command key to look up in CMD_PERMISSIONS.
                  ``None`` means the command has no permission restriction.
        rest:     Remainder of the line (arguments + description).
        perms_set: Set of permissions the current user holds, or ``None``
                   when the user is not logged in (→ show all commands normally).
    """
    restricted = (
        cmd_key is not None
        and perms_set is not None
        and CMD_PERMISSIONS.get(cmd_key, "") not in perms_set
    )
    if restricted:
        return f"  [dim]{syntax}{rest} [italic](sem acesso)[/italic][/dim]"
    return f"  [bright_yellow]{syntax}[/bright_yellow]{rest}"


def show_help(console: Console, permissions: list[str] | None = None) -> None:
    """Display help information.

    Args:
        console: Rich console instance.
        permissions: List of permissions from ``/me`` (or JWT), or ``None``
            when not logged in.  Commands the user lacks access to are shown
            dimmed with an ``(sem acesso)`` label.
    """
    p: set[str] | None = set(permissions) if permissions is not None else None

    lines: list[str] = [
        "[bold bright_cyan]gSage AI — CLI Client[/bold bright_cyan]",
        "",
        "[bold]Authentication:[/bold]",
        "",
        _c("login", None, " [email] - Log in with email + password (prompts for password; handles OTP if required)", p),
        _c("register", None, " - Create a new account and organization interactively", p),
        _c("whoami", None, " - Show current logged-in user and org memberships", p),
        _c("profile update", None, " - Change display name", p),
        _c("profile change-password", None, " - Change account password", p),
        "",
        "[bold]Two-Factor Authentication (OTP):[/bold]",
        "",
        _c("otp status", None, " - Show 2FA enrollment status", p),
        _c("otp enable", None, " - Enable TOTP (QR setup + app confirmation)", p),
        _c("otp disable", None, " - Disable TOTP (confirms with password or OTP code)", p),
        _c("otp backup-codes regenerate", None, " - Generate new backup codes (invalidates old ones)", p),
        "",
        "[bold]Conversations:[/bold]",
        "",
        _c("conversation list", "conversation", r" \[page] \[limit] - List conversations", p),
        _c("conversation new [title]", "conversation", " - Create a new conversation with optional title", p),
        _c("conversation <id>", "conversation", " - Switch to a specific conversation by ID", p),
        _c("conversation archive <id>", "conversation", " - Archive a conversation", p),
        _c("messages [limit]", "messages", " - Show recent messages (default: 10)", p),
        "",
        "[bold]Knowledge base:[/bold]",
        "",
        _c("knowledge search <query>", "knowledge", " - Semantic search over the knowledge base", p),
        _c("knowledge list", "knowledge", r" \[page] \[limit] - List stored documents", p),
        _c("knowledge add <name>", "knowledge", " - Add a text document (prompts for content)", p),
        _c("knowledge delete <id>", "knowledge", " - Delete a document by ID", p),
        _c("knowledge ingest <file>", "knowledge", r" \[--scope org|user] - Upload a file for async ingestion (pdf, docx, html, json, txt, md, csv, xlsx, pptx, zip, tar.gz, \u2026)", p),
        _c("knowledge status <job_id>", "knowledge", " - Check ingest job status", p),
        "",
        "[bold]Approvals (HITL):[/bold]",
        "",
        _c("approvals list", "approvals", " [status] - List approvals (optionally filter: pending/approved/rejected)", p),
        _c("approvals show <id>", "approvals", " - Show approval details", p),
        _c("approvals approve <id> [comment]", "approvals", " - Approve a pending request", p),
        _c("approvals reject  <id> [comment]", "approvals", " - Reject a pending request", p),
        "",
        "[bold]Files:[/bold]",
        "",
        _c("files list", "files", r" \[page] \[limit] \[--tool name] \[--all] \[--category generated|template] - List files", p),
        _c("files download <id> [dest]", "files", " - Download a file (saved to ./gsage_ai_downloads/ by default)", p),
        _c('files upload <path>', "files", r' \[--description "text"] \[--scope user|org|dept] - Upload a document template', p),
        _c("files delete <id>", "files", " - Delete a document template (prompts confirmation)", p),
        "",
        "[bold]Background Tasks:[/bold]",
        "",
        _c("tasks list", "tasks", r" \[page] \[limit] \[--tool name] \[--status status] - List background task executions", p),
        "",
        "[bold]Approval Rules (admin):[/bold]",
        "",
        _c("approval-rules list", "approval-rules", r" \[page] \[limit] \[--active true|false] \[--tool PATTERN] - List rules", p),
        _c("approval-rules show <id>", "approval-rules", " - Show rule details", p),
        _c("approval-rules create --tool PATTERN --approver USER_ID", "approval-rules", r' \[--user USER_ID|*] \[--dept UUID|*] \[--priority N] \[--desc "..."] - Create rule', p),
        _c("approval-rules update <id>", "approval-rules", r' \[--tool PATTERN] \[--approver USER_ID] \[--user USER_ID|*] \[--dept UUID|*] \[--priority N] \[--desc "..."] - Update rule', p),
        _c("approval-rules activate <id>", "approval-rules", " - Activate a rule", p),
        _c("approval-rules deactivate <id>", "approval-rules", " - Deactivate a rule", p),
        _c("approval-rules delete <id>", "approval-rules", " - Delete a rule (prompts confirmation)", p),
        "",
        "[bold]DataStores:[/bold]",
        "",
        _c("datastores list", "datastores", r" \[page] \[limit] - List data stores", p),
        _c("datastores show <store_id>", "datastores", " - Show data store details", p),
        _c('datastores create --name "..."', "datastores", r' \[--desc "..."] \[--visibility shared|private] \[--max-records N] \[--schema \'{}\'] - Create store', p),
        _c("datastores update <store_id>", "datastores", r' \[--name "..."] \[--desc "..."] \[--visibility shared|private] \[--max-records N] \[--activate|--deactivate] - Update store', p),
        _c("datastores delete <store_id>", "datastores", " - Delete store and all records (prompts confirmation)", p),
        _c("datastores records <store_id>", "datastores", r" \[page] \[limit] - List records in a store", p),
        _c("datastores record <store_id> <record_id>", "datastores", " - Show a single record", p),
        _c("datastores add-record <store_id> <json_data>", "datastores", " - Insert a new record", p),
        _c("datastores update-record <store_id> <record_id> <json_data>", "datastores", " - Update a record", p),
        _c("datastores delete-record <store_id> <record_id>", "datastores", " - Delete a record (prompts confirmation)", p),
        _c("datastores query <store_id>", "datastores", r" \[json_filters] - Query records with optional filters", p),
        "",
        "[bold]Departments:[/bold]",
        "",
        _c("dept list", None, " - List all departments in the current org", p),
        _c("dept my", None, " - List your department memberships", p),
        _c("dept info", None, " - Show the currently active department", p),
        _c("dept set <id|slug>", None, " - Switch active department by UUID or slug", p),
        "",
        "[bold]API Keys (admin):[/bold]",
        "",
        _c("api-keys list", "api-keys", r" \[page] \[limit] - List API keys for the org", p),
        "",
        "[bold]AI Agents (admin):[/bold]",
        "",
        _c("agents list", "agents", r" \[page] \[limit] \[--type PROMPT_RUN|SYSTEM_TASK] \[--active true|false] - List AI agents", p),
        _c("agents show <id>", "agents", " - Show an AI agent's details", p),
        _c('agents create --name "..." --cron "..." --prompt "..."', "agents", r' \[--tz TZ] \[--desc "..."] - Create a PROMPT_RUN AI agent', p),
        _c("agents update <id>", "agents", r' \[--name "..."] \[--cron "..."] \[--prompt "..."] \[--tz TZ] \[--desc "..."] \[--max-runs N] - Update a job', p),
        _c("agents activate <id>", "agents", " - Activate an AI agent (sync to RedBeat)", p),
        _c("agents deactivate <id>", "agents", " - Deactivate an AI agent (remove from RedBeat)", p),
        _c("agents delete <id>", "agents", " - Permanently delete an AI agent", p),
        "",
        "[bold]Org Admin (requires admin:access):[/bold]",
        "",
        _c("admin org", "admin", " - Show organization settings", p),
        _c("admin org update --name NAME", "admin", r' \[--slug SLUG] \[--llm-provider PROVIDER] \[--llm-api-key KEY] - Update org settings', p),
        _c("admin users list", "admin", r" \[--search TEXT] \[page] \[limit] - List org members", p),
        _c("admin users show <id>", "admin", " - Show member details", p),
        _c("admin users create --email E --name N", "admin", r' \[--role user|admin] - Invite a new user', p),
        _c("admin users reset-password <id>", "admin", " - Reset user password (returns temp password)", p),
        _c("admin users reset-otp <id>", "admin", " - Disable OTP for a user", p),
        _c("admin users remove <id>", "admin", " - Remove a user from the org", p),
        _c("admin groups list", "admin", " - List permission groups", p),
        _c("admin groups show <id>", "admin", " - Show group details", p),
        _c("admin groups create --name N", "admin", r' \[--desc "..."] - Create a group', p),
        _c("admin groups delete <id>", "admin", " - Delete a group", p),
        _c("admin groups permissions", "admin", " - List available permissions", p),
        _c("admin tool-configs list", "admin", " - List tool config overrides", p),
        _c("admin tool-configs create --tool T --profile P --config '{}'", "admin", r' \[--desc "..."] - Create a tool config', p),
        _c("admin tool-configs delete <id>", "admin", " - Delete a tool config", p),
        _c("admin interfaces list", "admin", " - List interface profiles", p),
        _c("admin interfaces create --interface I", "admin", r' \[--mode allowlist|denylist] \[--tags t1,t2] - Create an interface profile', p),
        _c("admin interfaces delete <id>", "admin", " - Delete an interface profile", p),
        _c("admin emails list", "admin", " - List email accounts", p),
        _c("admin emails create --email E --imap-host H --smtp-host H", "admin", " - Create an email account", p),
        _c("admin emails test <id>", "admin", " - Test IMAP/SMTP connectivity", p),
        _c("admin emails delete <id>", "admin", " - Delete an email account", p),
        "",
        "[bold]Attachments:[/bold]",
        "",
        _c("attach <path>", None, " - Stage a local file as an attachment for the next message", p),
        "  (the attachment is sent together with the next message you type)",
        "",
        "[bold]Composing messages:[/bold]",
        "",
        _c("editor", None, " - Open an external editor to compose a multi-line message", p),
        "  (uses $VISUAL or $EDITOR; falls back to nano / vim / vi)",
        "",
        "[bold]Other:[/bold]",
        "",
        _c("debug", None, " - Toggle debug mode on/off", p),
        _c("clear", None, " - Clear the screen", p),
        "  [bright_yellow]exit[/bright_yellow] or [bright_yellow]quit[/bright_yellow] - Exit the CLI",
        "",
        "[bold]Default behavior:[/bold]",
        "  Any text that is not a command is sent as a message to the current conversation.",
        "",
        "[bold]Examples:[/bold]",
        "  > login admin@example.com",
        "  > conversation list",
        "  > conversation new Security Investigation",
        "  > conversation archive 7c8b8405-0715-4c56-a517-3afe78a7dff5",
        "  > messages 5",
        "  > What is a DNS lookup?",
    ]

    help_text = "\n".join(lines)
    console.print(Panel(help_text, border_style=COLOR_INFO))

    if permissions is not None:
        _show_permission_status(console, permissions)


def _show_permission_status(console: Console, permissions: list[str]) -> None:
    """Show a compact permission status table for the current session."""
    _PERM_MAP: list[tuple[str, str]] = [
        ("conversation / messages", "sessions:read"),
        ("knowledge", "knowledge:read"),
        ("approvals", "approvals:read"),
        ("files", "files:upload"),
        ("tasks", "agents:run"),
        ("agents", "scheduled_jobs:read"),
        ("approval-rules", "approval_rules:read"),
        ("datastores", "datastores:read"),
        ("api-keys", "apikeys:personal"),
        ("admin", "admin:access"),
    ]
    table = Table(
        title="[bold]Command Access (current session)[/bold]",
        show_header=True,
        header_style=f"bold {COLOR_INFO}",
        border_style="dim",
    )
    table.add_column("Command", style="bright_yellow", no_wrap=True)
    table.add_column("Required Permission", style="dim")
    table.add_column("Access", no_wrap=True)
    for cmd, perm in _PERM_MAP:
        if perm in permissions:
            access = "[bright_green]✓ Available[/bright_green]"
        else:
            access = "[dim]✗ Restricted[/dim]"
        table.add_row(cmd, perm, access)
    console.print(table)


# ---------------------------------------------------------------------------
# Auth commands
# ---------------------------------------------------------------------------


def handle_login_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'login [email]' — prompts for password interactively."""
    email = args[0] if args else None
    if not email:
        try:
            email = input("Email: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Login cancelled.[/{COLOR_INFO}]")
            return
    if not email:
        console.print(f"[{COLOR_ERROR}]Email is required.[/{COLOR_ERROR}]")
        return

    try:
        password = getpass.getpass("Password: ")
    except (KeyboardInterrupt, EOFError):
        console.print(f"\n[{COLOR_INFO}]Login cancelled.[/{COLOR_INFO}]")
        return

    try:
        result = client.login(email=email, password=password)
    except Exception as exc:
        console.print(f"[{COLOR_ERROR}]Login failed: {exc}[/{COLOR_ERROR}]")
        if state.debug:
            logger.exception("login failed")
        return

    if result.get("otp_required"):
        otp_token: str = result["otp_token"]
        not_enrolled: bool = result.get("otp_not_enrolled", False)
        if not_enrolled:
            console.print(
                f"[{COLOR_INFO}]Your organization requires 2FA but you have not set it up yet.[/{COLOR_INFO}]"
            )
            console.print(f"[{COLOR_INFO}]Run [bold]otp enable[/bold] after logging in to configure.[/{COLOR_INFO}]")
        console.print(f"[{COLOR_INFO}]Two-factor authentication required.[/{COLOR_INFO}]")
        try:
            code = input("Enter OTP code (or backup code): ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Login cancelled.[/{COLOR_INFO}]")
            return
        if not code:
            console.print(f"[{COLOR_ERROR}]OTP code required.[/{COLOR_ERROR}]")
            return

        try:
            remember_raw = input("Remember this device? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            remember_raw = "n"
        remember = remember_raw in ("y", "yes")

        # Determine if it looks like a backup code (long) vs TOTP (6 digits)
        is_backup = len(code) > 8
        try:
            client.verify_otp(
                otp_token=otp_token,
                code=None if is_backup else code,
                backup_code=code if is_backup else None,
                remember_device=remember,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]OTP verification failed: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("otp verify failed")
            return

    console.print(f"[{COLOR_SUCCESS}]✓ Logged in as {email}[/{COLOR_SUCCESS}]")
    if client.org_id:
        console.print(f"[dim]Org ID: {client.org_id}[/dim]")


def handle_register_command(
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'register' — prompts for all fields interactively."""
    console.print(f"[{COLOR_INFO}]Create a new account and organization[/{COLOR_INFO}]")
    try:
        email = input("Email: ").strip()
        full_name = input("Full name: ").strip()
        org_name = input("Organization name: ").strip()
        password = getpass.getpass("Password (min 8 chars): ")
        password2 = getpass.getpass("Confirm password: ")
    except (KeyboardInterrupt, EOFError):
        console.print(f"\n[{COLOR_INFO}]Registration cancelled.[/{COLOR_INFO}]")
        return

    if not all([email, full_name, org_name, password]):
        console.print(f"[{COLOR_ERROR}]All fields are required.[/{COLOR_ERROR}]")
        return
    if password != password2:
        console.print(f"[{COLOR_ERROR}]Passwords do not match.[/{COLOR_ERROR}]")
        return

    try:
        client.register(
            email=email,
            password=password,
            full_name=full_name,
            org_name=org_name,
        )
        console.print(f"[{COLOR_SUCCESS}]✓ Account created and logged in as {email}[/{COLOR_SUCCESS}]")
        if client.org_id:
            console.print(f"[dim]Org ID: {client.org_id}[/dim]")
    except Exception as exc:
        console.print(f"[{COLOR_ERROR}]Registration failed: {exc}[/{COLOR_ERROR}]")
        if state.debug:
            logger.exception("register failed")


def handle_otp_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'otp <subcommand>' — manage TOTP two-factor authentication.

    Subcommands:
      status                     — show OTP enrollment status
      enable                     — interactive setup: scan QR then confirm
      disable                    — disable OTP (prompts for password or current code)
      backup-codes regenerate    — generate a new set of backup codes
    """
    subcommand = args[0].lower() if args else "status"

    # ── status ────────────────────────────────────────────────────────────
    if subcommand == "status":
        try:
            data = client.otp_status()
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get OTP status: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("otp_status failed")
            return
        enabled = data.get("enabled", False)
        badge = f"[{COLOR_SUCCESS}]enabled[/{COLOR_SUCCESS}]" if enabled else f"[{COLOR_ERROR}]disabled[/{COLOR_ERROR}]"
        console.print(f"[{COLOR_INFO}]Two-factor authentication:[/{COLOR_INFO}] {badge}")
        if enabled:
            confirmed_at = data.get("confirmed_at") or "unknown"
            remaining = data.get("backup_codes_remaining", 0)
            console.print(f"[dim]Enabled at:       {confirmed_at}[/dim]")
            console.print(f"[dim]Backup codes left: {remaining}[/dim]")
        return

    # ── enable ────────────────────────────────────────────────────────────
    if subcommand == "enable":
        console.print(f"[{COLOR_INFO}]Starting OTP setup — you will need an authenticator app.[/{COLOR_INFO}]")
        try:
            setup = client.otp_setup()
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Setup failed: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("otp_setup failed")
            return

        uri = setup.get("provisioning_uri", "")
        secret = setup.get("secret", "")
        console.print(f"\n[{COLOR_INFO}]Scan the QR code in your authenticator app, or enter the secret manually:[/{COLOR_INFO}]")
        console.print(f"[bold]Secret:[/bold] {secret}")
        console.print(f"[dim]Provisioning URI: {uri}[/dim]\n")

        # Prompt for confirmation code
        try:
            code = input("Enter the 6-digit code from your app to confirm: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Setup cancelled.[/{COLOR_INFO}]")
            return
        if not code:
            console.print(f"[{COLOR_ERROR}]Code required.[/{COLOR_ERROR}]")
            return

        try:
            result = client.otp_confirm(code)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Confirmation failed: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("otp_confirm failed")
            return

        backup_codes: list[str] = result.get("backup_codes", [])
        console.print(f"[{COLOR_SUCCESS}]✓ Two-factor authentication enabled![/{COLOR_SUCCESS}]")
        if backup_codes:
            console.print(f"\n[{COLOR_INFO}]Save these backup codes somewhere safe:[/{COLOR_INFO}]")
            for bc in backup_codes:
                console.print(f"  {bc}")
            console.print(f"[dim]Each code can only be used once.[/dim]\n")
        return

    # ── disable ───────────────────────────────────────────────────────────
    if subcommand == "disable":
        console.print(f"[{COLOR_INFO}]Disabling OTP requires your password or a current OTP code.[/{COLOR_INFO}]")
        try:
            password = getpass.getpass("Password (leave blank to use OTP code instead): ")
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return

        otp_code: str | None = None
        if not password:
            try:
                otp_code = input("OTP code: ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
                return
            if not otp_code:
                console.print(f"[{COLOR_ERROR}]Password or OTP code required.[/{COLOR_ERROR}]")
                return

        try:
            client.otp_disable(
                password=password or None,
                otp_code=otp_code,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to disable OTP: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("otp_disable failed")
            return

        console.print(f"[{COLOR_SUCCESS}]✓ Two-factor authentication disabled.[/{COLOR_SUCCESS}]")
        return

    # ── backup-codes regenerate ────────────────────────────────────────────
    if subcommand == "backup-codes" and len(args) >= 2 and args[1].lower() == "regenerate":
        console.print(f"[{COLOR_INFO}]Regenerating backup codes — existing codes will be invalidated.[/{COLOR_INFO}]")
        try:
            password = getpass.getpass("Password (leave blank to use OTP code instead): ")
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return

        otp_code_regen: str | None = None
        if not password:
            try:
                otp_code_regen = input("OTP code: ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
                return
            if not otp_code_regen:
                console.print(f"[{COLOR_ERROR}]Password or OTP code required.[/{COLOR_ERROR}]")
                return

        try:
            result = client.regenerate_backup_codes(
                password=password or None,
                otp_code=otp_code_regen,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to regenerate codes: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("regenerate_backup_codes failed")
            return

        new_codes: list[str] = result.get("backup_codes", [])
        console.print(f"[{COLOR_SUCCESS}]✓ New backup codes generated:[/{COLOR_SUCCESS}]")
        for bc in new_codes:
            console.print(f"  {bc}")
        console.print(f"[dim]Each code can only be used once. Old codes are now invalid.[/dim]")
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown OTP subcommand '{subcommand}'. "
        f"Use: status | enable | disable | backup-codes regenerate[/{COLOR_ERROR}]"
    )


def handle_whoami_command(
    client: "GSageAPIClient",
    console: Console,
    state: "REPLState",
) -> None:
    """Handle 'whoami' — show current user and org."""
    try:
        me = client.get_me()
        console.print(
            f"[{COLOR_INFO}]User:[/{COLOR_INFO}] {me.get('email', '?')} ({me.get('full_name', '')})"
        )
        console.print(f"[{COLOR_INFO}]Org ID:[/{COLOR_INFO}] {client.org_id or 'not set'}")
        memberships = me.get("memberships", [])
        if memberships:
            console.print(f"[{COLOR_INFO}]Organizations:[/{COLOR_INFO}]")
            for m in memberships:
                marker = " ← active" if str(m.get("org_id")) == str(client.org_id) else ""
                console.print(
                    f"  {m.get('org_name', '?')} ({m.get('org_id', '?')}) — {m.get('role', '?')}{marker}"
                )
    except Exception as exc:
        console.print(f"[{COLOR_ERROR}]Failed to fetch user info: {exc}[/{COLOR_ERROR}]")
        if state.debug:
            logger.exception("whoami failed")


def handle_profile_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'profile update' and 'profile change-password'."""
    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: profile <update|change-password>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    if subcommand == "update":
        try:
            me = client.get_me()
            current_name = me.get("full_name") or ""
        except Exception:
            current_name = ""

        console.print(f"[{COLOR_INFO}]Current name:[/{COLOR_INFO}] {current_name or '(none)'}")
        try:
            new_name = input("New full name: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        if not new_name:
            console.print(f"[{COLOR_ERROR}]Name cannot be empty.[/{COLOR_ERROR}]")
            return

        try:
            updated = client.update_profile(full_name=new_name)
            console.print(
                f"[{COLOR_SUCCESS}]✓ Profile updated.[/{COLOR_SUCCESS}] "
                f"Name: {updated.get('full_name', new_name)}"
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to update profile: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("update_profile failed")

    elif subcommand in ("change-password", "passwd", "password"):
        try:
            current_pwd = getpass.getpass("Current password: ")
            new_pwd = getpass.getpass("New password (min 8 chars): ")
            confirm_pwd = getpass.getpass("Confirm new password: ")
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return

        if not all([current_pwd, new_pwd, confirm_pwd]):
            console.print(f"[{COLOR_ERROR}]All fields are required.[/{COLOR_ERROR}]")
            return
        if new_pwd != confirm_pwd:
            console.print(f"[{COLOR_ERROR}]Passwords do not match.[/{COLOR_ERROR}]")
            return

        try:
            client.change_password(current_password=current_pwd, new_password=new_pwd)
            console.print(f"[{COLOR_SUCCESS}]✓ Password changed successfully.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to change password: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("change_password failed")

    else:
        console.print(
            f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. "
            f"Use: profile update | profile change-password[/{COLOR_ERROR}]"
        )


# ---------------------------------------------------------------------------
# Conversation commands
# ---------------------------------------------------------------------------


def handle_conversation_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'conversation' subcommands."""
    if not args:
        console.print(f"[{COLOR_ERROR}]Usage: conversation <list|new|show|archive> [id][/{COLOR_ERROR}]")
        return

    subcommand = args[0].lower()

    if subcommand == "list":
        page = 1
        limit = 20
        try:
            if len(args) >= 2:
                page = int(args[1])
            if len(args) >= 3:
                limit = int(args[2])
        except ValueError:
            console.print(f"[{COLOR_ERROR}]Usage: conversation list \\[[page] \\[[limit][/{COLOR_ERROR}]")
            return

        try:
            data = client.list_conversations(page=page, limit=limit)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list conversations: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_conversations failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No conversations found.[/{COLOR_INFO}]")
            return

        def _build_conv_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"Conversations  ({total} total)")
            tbl.add_column("Created", style="dim", no_wrap=True)
            tbl.add_column("ID", style="bright_cyan", no_wrap=True)
            tbl.add_column("Title", style="bright_yellow")
            tbl.add_column("Status", justify="center", style="bright_blue")
            for conv in items:
                conv_id = str(conv["id"])
                title = conv.get("title") or "(no title)"
                is_active = conv.get("is_active", True)
                status_str = "active" if is_active else "archived"
                created_at = conv.get("created_at", "")
                created_date = created_at[:10] if created_at else "unknown"
                if state.conversation_id and str(conv["id"]) == str(state.conversation_id):
                    conv_id = f"[bold]{conv_id}[/bold] ←"
                tbl.add_row(created_date, conv_id, title, status_str)
            return tbl

        render_paginated_table(
            console,
            data,
            _build_conv_table,
            command_hint="conversation list",
            fetch_fn=lambda pg: client.list_conversations(page=pg, limit=limit),
        )
        return

    if subcommand == "new":
        title = " ".join(args[1:]) if len(args) > 1 else None
        try:
            conv = client.create_conversation(title=title)
            state.conversation_id = str(conv["id"])

            title_display = conv.get("title") or str(conv["id"])
            console.print(f"[{COLOR_SUCCESS}]✓ Created conversation: {title_display}[/{COLOR_SUCCESS}]")

            if state.debug:
                console.print(f"[dim]Conversation ID: {conv['id']}[/dim]")

        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to create conversation: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("create_conversation failed")
        return

    if subcommand == "show":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: conversation show <id>[/{COLOR_ERROR}]")
            return
        conversation_id = args[1]
        try:
            conv = client.get_conversation(conversation_id)
            state.conversation_id = str(conv["id"])
            title_display = conv.get("title") or str(conv["id"])
            console.print(f"[{COLOR_SUCCESS}]✓ Switched to conversation: {title_display}[/{COLOR_SUCCESS}]")
            if state.debug:
                console.print(f"[dim]Conversation ID: {conv['id']}[/dim]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to switch conversation: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("get_conversation failed")
        return

    if subcommand == "archive":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: conversation archive <id>[/{COLOR_ERROR}]")
            return

        conversation_id = args[1]
        try:
            conv = client.archive_conversation(conversation_id)

            title_display = conv.get("title") or str(conv["id"])
            console.print(f"[{COLOR_SUCCESS}]✓ Archived conversation: {title_display}[/{COLOR_SUCCESS}]")

            if state.conversation_id and str(conv["id"]) == str(state.conversation_id):
                state.conversation_id = None
                console.print("[dim]Active conversation cleared.[/dim]")

            if state.debug:
                console.print(f"[dim]Conversation ID: {conv['id']}[/dim]")

        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to archive conversation: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("archive_conversation failed")
        return

    console.print(f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. Usage: conversation <list|new|show|archive>[/{COLOR_ERROR}]")


def handle_messages_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'messages [limit]' — list conversation history."""
    if not state.conversation_id:
        console.print(
            f"[{COLOR_ERROR}]No active conversation. Create one with: conversation new[/{COLOR_ERROR}]"
        )
        return

    last_n = 10
    if args:
        try:
            last_n = int(args[0])
        except ValueError:
            console.print(f"[{COLOR_ERROR}]Invalid limit. Using default: 10[/{COLOR_ERROR}]")

    try:
        messages = client.list_messages(state.conversation_id, last_n=last_n)

        if not messages:
            console.print(f"[{COLOR_INFO}]No messages in this conversation yet.[/{COLOR_INFO}]")
            return

        console.print(f"\n[bold]Last {len(messages)} messages:[/bold]\n")

        for msg in messages:
            role = msg.get("role", "").upper()
            content = msg.get("content", "")

            # Skip internal background-task injection blocks (not user-visible)
            if role == "USER" and content.lstrip().startswith("[BACKGROUND_TASKS_COMPLETED]"):
                continue

            if role == "USER":
                color = COLOR_USER
                prefix = "YOU"
            elif role == "ASSISTANT":
                color = COLOR_ASSISTANT
                prefix = "ASSISTANT"
            else:
                color = "white"
                prefix = role

            console.print(f"[bold {color}]{prefix}:[/bold {color}]")

            if role == "ASSISTANT" and state.output_format == "markdown":
                console.print(Markdown(content))
            else:
                console.print(content, markup=False)

            console.print()  # blank line between messages

    except Exception as exc:
        console.print(f"[{COLOR_ERROR}]Failed to list messages: {rich_escape(str(exc))}[/{COLOR_ERROR}]")
        if state.debug:
            logger.exception("list_messages failed")


def handle_send_message(
    message: str,
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
    attachment_ids: list[str] | None = None,
) -> None:
    """Send a message to the current conversation using SSE streaming."""
    if not state.conversation_id:
        # Auto-create a conversation on first message
        try:
            conv = client.create_conversation(title=None)
            state.conversation_id = str(conv["id"])

            if state.debug:
                console.print(f"[dim]Created conversation: {conv['id']}[/dim]")

        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to create conversation: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("auto-create conversation failed")
            return

    # Display user message
    console.print(f"\n[bold {COLOR_USER}]YOU:[/bold {COLOR_USER}]")
    console.print(message)
    console.print()

    assert state.conversation_id is not None

    console.print(f"[bold {COLOR_ASSISTANT}]ASSISTANT:[/bold {COLOR_ASSISTANT}]")

    buffer = ""
    paused = False
    pending_approvals: list[str] = []
    paused_run_id: str | None = None

    effective_attachment_ids = attachment_ids or []

    try:
        # Stream as plain Text inside Live (Markdown re-rendering on every
        # delta breaks Live's overwrite logic — the rendered height changes
        # mid-frame and Rich appends instead of overwriting, producing the
        # "same line repeated" artefact in the terminal).  Render the final
        # Markdown once after the Live block exits.
        with Live(
            "",
            console=console,
            refresh_per_second=15,
            vertical_overflow="ellipsis",
            transient=True,
        ) as live:
            for event_name, data in client.stream_message(
                state.conversation_id,
                message,
                attachment_ids=effective_attachment_ids if effective_attachment_ids else None,
            ):
                if event_name == "content_delta":
                    buffer += data.get("delta", "")
                    live.update(buffer)

                elif event_name == "run_paused":
                    pending_approvals = cast(list[str], data.get("pending_approvals") or [])
                    paused_run_id = data.get("run_id")
                    paused = True
                    break

                elif event_name == "message_end":
                    break

                elif event_name == "error":
                    live.stop()
                    console.print(f"[{COLOR_ERROR}]{data.get('detail', 'Streaming error')}[/{COLOR_ERROR}]")
                    return

        # Live block exited cleanly — render the final buffer (markdown if
        # enabled).  ``transient=True`` cleared the streamed plain text so
        # we can replace it with a properly formatted version.
        if buffer:
            if state.output_format == "markdown":
                console.print(Markdown(buffer))
            else:
                console.print(buffer, markup=False)

        if state.debug and buffer:
            console.print(f"[dim]Streamed {len(buffer)} chars[/dim]")

        if paused:
            console.print()
            console.print(f"[bold {COLOR_INFO}]⏸  Agent paused — awaiting approval[/bold {COLOR_INFO}]")
            for appr_id in pending_approvals:
                console.print(f"  [dim]Approval ID:[/dim] [bright_cyan]{appr_id}[/bright_cyan]")
            console.print(f"[dim]  → approvals approve <id>  or  approvals reject <id>[/dim]")
            if paused_run_id and state.debug:
                console.print(f"[dim]  Run ID: {paused_run_id}[/dim]")

        console.print()

    except Exception as exc:
        console.print(f"[{COLOR_ERROR}]Failed to send message: {exc}[/{COLOR_ERROR}]")
        if state.debug:
            logger.exception("stream_message failed")


# ---------------------------------------------------------------------------
# Knowledge base commands
# ---------------------------------------------------------------------------


def handle_knowledge_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'knowledge' subcommands.

    knowledge search <query>
    knowledge list [page] [limit]
    knowledge add <name> [--url <url>] [--description <desc>]
    knowledge delete <id>
    knowledge ingest <file> [--scope org|user]  (pdf, docx, html, json, txt, md, csv, xlsx, pptx, zip, tar.gz, …)
    knowledge status <job_id>
    """
    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: knowledge <search|list|add|delete|ingest|status>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    # ── search ──────────────────────────────────────────────────────────
    if subcommand == "search":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: knowledge search <query>[/{COLOR_ERROR}]")
            return
        query = " ".join(args[1:])
        try:
            data = client.search_knowledge(query=query)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Search failed: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("search_knowledge failed")
            return

        results = data.get("results", [])
        total = data.get("total", len(results))
        if not results:
            console.print(f"[{COLOR_INFO}]No results found for: {query}[/{COLOR_INFO}]")
            return

        console.print(f"[{COLOR_INFO}]Found {total} result(s):[/{COLOR_INFO}]\n")
        for i, r in enumerate(results, 1):
            score = r.get("score")
            score_str = f" (score: {score:.3f})" if score is not None else ""
            name = r.get("name") or r.get("id", "?")
            console.print(f"[bold {COLOR_INFO}]{i}. {name}{score_str}[/bold {COLOR_INFO}]")
            content = r.get("content", "")
            if content:
                preview = content[:400].replace("\n", " ")
                if len(content) > 400:
                    preview += "..."
                console.print(f"   {preview}")
            console.print()
        return

    # ── list ────────────────────────────────────────────────────────────
    if subcommand == "list":
        page = 1
        limit = 20
        try:
            if len(args) >= 2:
                page = int(args[1])
            if len(args) >= 3:
                limit = int(args[2])
        except ValueError:
            console.print(f"[{COLOR_ERROR}]Usage: knowledge list \\[page] \\[limit][/{COLOR_ERROR}]")
            return

        try:
            data = client.list_knowledge(page=page, limit=limit)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list knowledge: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_knowledge failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No documents found in the knowledge base.[/{COLOR_INFO}]")
            return

        def _build_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"Knowledge Base  ({total} document(s))")
            tbl.add_column("ID", style="bright_cyan", no_wrap=True)
            tbl.add_column("Name", style="bright_yellow")
            tbl.add_column("Type", style="dim", justify="center")
            tbl.add_column("Size", style="dim", justify="right")
            tbl.add_column("Status", justify="center")
            for item in items:
                doc_id = str(item.get("id", ""))
                name = item.get("name") or "-"
                doc_type = item.get("type") or "-"
                size = item.get("size")
                size_str = f"{size:,}" if size is not None else "-"
                st = item.get("status") or "-"
                color = "bright_green" if st == "active" else "bright_red"
                tbl.add_row(doc_id, name, doc_type, size_str, f"[{color}]{st}[/{color}]")
            return tbl

        render_paginated_table(
            console,
            data,
            _build_table,
            command_hint="knowledge list",
            fetch_fn=lambda pg: client.list_knowledge(page=pg, limit=limit),
        )
        return

    # ── add ─────────────────────────────────────────────────────────────
    if subcommand == "add":
        # Parse flags: --url <url> and --description <desc>
        remaining = args[1:]
        url: str | None = None
        description: str | None = None
        positional: list[str] = []
        i = 0
        while i < len(remaining):
            if remaining[i] == "--url" and i + 1 < len(remaining):
                url = remaining[i + 1]
                i += 2
            elif remaining[i] == "--description" and i + 1 < len(remaining):
                description = remaining[i + 1]
                i += 2
            else:
                positional.append(remaining[i])
                i += 1

        if not positional:
            console.print(
                f"[{COLOR_ERROR}]Usage: knowledge add <name> [--url <url>] [--description <desc>][/{COLOR_ERROR}]"
            )
            return

        name = " ".join(positional)

        content: str | None = None
        if url:
            # Content is optional when URL provided
            console.print(
                f"[{COLOR_INFO}]URL provided — content will be fetched automatically. "
                f"Optionally paste additional content below (Ctrl+D to skip/finish):[/{COLOR_INFO}]"
            )
        else:
            console.print(
                f"[{COLOR_INFO}]Paste content below. Enter a blank line followed by EOF (Ctrl+D) to finish:[/{COLOR_INFO}]"
            )

        lines: list[str] = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        except KeyboardInterrupt:
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return

        typed_content = "\n".join(lines).strip()
        if typed_content:
            content = typed_content
        elif not url:
            console.print(f"[{COLOR_ERROR}]Empty content — nothing added.[/{COLOR_ERROR}]")
            return

        try:
            doc = client.add_knowledge(name=name, content=content, description=description, url=url)
            console.print(f"[{COLOR_SUCCESS}]✓ Document added: {doc.get('id')}[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to add document: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("add_knowledge failed")
        return

    # ── delete ──────────────────────────────────────────────────────────
    if subcommand == "delete":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: knowledge delete <id>[/{COLOR_ERROR}]")
            return
        content_id = args[1]
        try:
            confirm = input(f"Delete document {content_id}? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        if confirm not in ("y", "yes"):
            console.print("[dim]Deletion cancelled.[/dim]")
            return
        try:
            client.delete_knowledge(content_id)
            console.print(f"[{COLOR_SUCCESS}]✓ Document {content_id} deleted.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to delete document: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("delete_knowledge failed")
        return

    # ── ingest ──────────────────────────────────────────────────────────
    if subcommand == "ingest":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: knowledge ingest <file> \\[--scope org|user|dept][/{COLOR_ERROR}]")
            return
        filepath = args[1]
        scope = "org"
        if "--scope" in args:
            idx = args.index("--scope")
            if idx + 1 < len(args):
                scope = args[idx + 1]
        try:
            result = client.ingest_document(filepath=filepath, scope=scope)
            job_id = result.get("job_id", "?")
            filename = result.get("filename", filepath)
            # Store the job ID so tab-complete works for 'knowledge status'
            if job_id != "?":
                state.ingest_jobs.insert(0, job_id)
                state.ingest_jobs[:] = state.ingest_jobs[:20]  # cap at 20
            console.print(
                f"[{COLOR_SUCCESS}]✓ Ingest queued: {filename}[/{COLOR_SUCCESS}]\n"
                f"[dim]Job ID: {job_id}[/dim]\n"
                f"[dim]Check status: knowledge status {job_id}[/dim]"
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to ingest document: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("ingest_document failed")
        return

    # ── status ──────────────────────────────────────────────────────────
    if subcommand == "status":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: knowledge status <job_id>[/{COLOR_ERROR}]")
            return
        job_id = args[1]
        try:
            job = client.get_ingest_status(job_id)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get job status: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("get_ingest_status failed")
            return

        st = job.get("status", "?")
        color = (
            "bright_green" if st in ("COMPLETED", "completed")
            else "bright_red" if st in ("FAILED", "failed")
            else "bright_yellow"
        )
        table = Table(show_header=False, box=None)
        table.add_column("Field", style="dim")
        table.add_column("Value")
        for field, value in [
            ("Job ID", job.get("job_id")),
            ("Status", f"[{color}]{st}[/{color}]"),
            ("File", job.get("filename")),
            ("Scope", job.get("scope")),
            ("Size", str(job.get("file_size") or "-")),
            ("Chunks stored", str(job.get("chunks_stored") or "-")),
            ("Error", job.get("error_message")),
        ]:
            if value and str(value) not in ("-", "None"):
                table.add_row(field, str(value))
        console.print(table)
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. "
        f"Use: search, list, add, delete, ingest, status[/{COLOR_ERROR}]"
    )


def toggle_debug(state: "REPLState", console: Console) -> None:
    """Toggle debug mode."""
    state.debug = not state.debug
    status_str = "enabled" if state.debug else "disabled"
    console.print(f"[{COLOR_INFO}]Debug mode {status_str}[/{COLOR_INFO}]")

    if state.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Approvals (HITL) commands
# ---------------------------------------------------------------------------


def handle_approvals_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'approvals' subcommands.

    approvals list [--status pending|approved|rejected]
    approvals show <id>
    approvals approve <id> [comment...]
    approvals reject  <id> [comment...]
    """
    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: approvals <list|show|approve|reject>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    # ── list ────────────────────────────────────────────────────────────
    if subcommand == "list":
        status_filter: str | None = None
        if len(args) >= 3 and args[1] in ("--status", "-s"):
            status_filter = args[2].lower()
        elif len(args) == 2 and args[1] not in ("--status", "-s"):
            # shorthand: approvals list pending
            status_filter = args[1].lower()

        try:
            data = client.list_approvals(approval_status=status_filter)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list approvals: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_approvals failed")
            return

        items = data.get("items", [])
        total = data.get("total", len(items))

        if not items:
            label = f" ({status_filter})" if status_filter else ""
            console.print(f"[{COLOR_INFO}]No approvals found{label}.[/{COLOR_INFO}]")
            return

        table = Table(title=f"Approvals — {total} total")
        table.add_column("ID", style="bright_cyan", no_wrap=True)
        table.add_column("Status", justify="center")
        table.add_column("Tool", style="bright_yellow")
        table.add_column("Created", style="dim", no_wrap=True)

        for item in items:
            approval_id = str(item.get("id", ""))
            st = item.get("status") or "?"
            color = (
                "bright_yellow" if st == "pending"
                else "bright_green" if st == "approved"
                else "bright_red"
            )
            tool = item.get("tool_name") or item.get("approval_type") or "-"
            created = str(item.get("created_at") or "")
            table.add_row(
                approval_id,
                f"[{color}]{st}[/{color}]",
                tool,
                created,
            )

        console.print(table)
        return

    # ── show ────────────────────────────────────────────────────────────
    if subcommand == "show":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: approvals show <id>[/{COLOR_ERROR}]")
            return
        approval_id = args[1]
        try:
            item = client.get_approval(approval_id)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get approval: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("get_approval failed")
            return

        st = item.get("status") or "?"
        color = (
            "bright_yellow" if st == "pending"
            else "bright_green" if st == "approved"
            else "bright_red"
        )
        table = Table(show_header=False, box=None)
        table.add_column("Field", style="dim")
        table.add_column("Value")

        for field, value in [
            ("ID", item.get("id")),
            ("Status", f"[{color}]{st}[/{color}]"),
            ("Tool", item.get("tool_name")),
            ("Tool args", str(item.get("tool_args") or "")),
            ("Agent", item.get("agent_id")),
            ("Run ID", item.get("run_id")),
            ("Session", item.get("session_id")),
            ("Resolved by", item.get("resolved_by")),
            ("Comment", str((item.get("resolution_data") or {}).get("comment", ""))),
            ("Created at", str(item.get("created_at") or "")),
        ]:
            if value:
                table.add_row(field, str(value))

        console.print(table)
        return

    # ── approve / reject ────────────────────────────────────────────────
    if subcommand in ("approve", "reject"):
        if len(args) < 2:
            console.print(
                f"[{COLOR_ERROR}]Usage: approvals {subcommand} <id> [comment][/{COLOR_ERROR}]"
            )
            return
        approval_id = args[1]
        comment = " ".join(args[2:]) if len(args) > 2 else None

        try:
            item = client.resolve_approval(
                approval_id=approval_id,
                action=subcommand,
                comment=comment,
            )
        except Exception as exc:
            console.print(
                f"[{COLOR_ERROR}]Failed to {subcommand} approval: {exc}[/{COLOR_ERROR}]"
            )
            if state.debug:
                logger.exception("resolve_approval failed")
            return

        new_status = item.get("status", subcommand + "d")
        verb = "Approved" if subcommand == "approve" else "Rejected"
        console.print(
            f"[{COLOR_SUCCESS}]✓ {verb} approval {approval_id} — status: {new_status}[/{COLOR_SUCCESS}]"
        )

        # After approving, automatically continue the paused run so the tool
        # actually executes and the audit log is generated.
        if subcommand == "approve":
            console.print(f"[{COLOR_INFO}]Resuming agent run...[/{COLOR_INFO}]")
            try:
                run_result = client.continue_run_from_approval(approval_id)
            except Exception as exc:
                console.print(
                    f"[{COLOR_ERROR}]Warning: approval resolved but run resume failed: {exc}[/{COLOR_ERROR}]"
                )
                if state.debug:
                    logger.exception("continue_run_from_approval failed")
                return

            run_status = run_result.get("status", "completed")
            content = run_result.get("content", "")

            if run_status == "pending_approval":
                pending = run_result.get("pending_approvals") or []
                console.print(
                    f"[bright_yellow]Run paused — additional approvals required:[/bright_yellow]"
                )
                for aid in pending:
                    console.print(f"  [{COLOR_INFO}]{aid}[/{COLOR_INFO}]")
            else:
                if content:
                    console.print(f"\n[{COLOR_SUCCESS}]Agent:[/{COLOR_SUCCESS}] {content}")
                else:
                    console.print(f"[{COLOR_SUCCESS}]Run completed.[/{COLOR_SUCCESS}]")

        return

    console.print(
        f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. "
        f"Use: list, show, approve, reject[/{COLOR_ERROR}]"
    )


# ---------------------------------------------------------------------------
# File commands
# ---------------------------------------------------------------------------


def handle_files_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'files' subcommands.

    files list [page] [limit] [--tool <name>] [--all] [--category generated|template]
    files download <id> [dest_path]
    files upload <path> [--description "text"] [--scope user|org|dept]
    files delete <id>
    """
    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: files <list|download|upload|delete>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    # ── list ────────────────────────────────────────────────────────────
    if subcommand == "list":
        page = 1
        limit = 20
        tool_name: str | None = None
        include_purged = False
        category: str | None = None
        remaining = args[1:]

        # parse flags first
        clean: list[str] = []
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--tool", "-t") and i + 1 < len(remaining):
                tool_name = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--all", "-a"):
                include_purged = True
                i += 1
            elif remaining[i] in ("--category", "-c") and i + 1 < len(remaining):
                category = remaining[i + 1]
                i += 2
            else:
                clean.append(remaining[i])
                i += 1

        try:
            if len(clean) >= 1:
                page = int(clean[0])
            if len(clean) >= 2:
                limit = int(clean[1])
        except ValueError:
            console.print(f"[{COLOR_ERROR}]Usage: files list \\[page] \\[limit] \\[--tool name] \\[--category generated|template][/{COLOR_ERROR}]")
            return

        try:
            data = client.list_files(
                page=page,
                limit=limit,
                tool_name=tool_name,
                include_purged=include_purged,
                category=category,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list files: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_files failed")
            return

        items = data.get("items", [])
        total = data.get("total", len(items))

        if not items:
            console.print(f"[{COLOR_INFO}]No files found.[/{COLOR_INFO}]")
            return

        table = Table(title=f"Files — {total} total (page {page})")
        table.add_column("ID", style="bright_cyan", no_wrap=True)
        table.add_column("Tool", style="bright_yellow")
        table.add_column("Filename", style="white")
        table.add_column("Category", style="dim")
        table.add_column("Scope", style="dim")
        table.add_column("Size", justify="right", style="dim")
        table.add_column("Expires / Status", style="dim")

        for item in items:
            full_id = str(item.get("id", ""))
            tool = item.get("tool_name") or "-"
            filename = item.get("filename") or "-"
            cat = item.get("category") or "generated"
            sc = item.get("scope") or "user"
            size_bytes = item.get("size_bytes")
            size_str = _format_bytes(size_bytes) if size_bytes else "-"

            purged_at = item.get("purged_at")
            expires_at = item.get("expires_at")
            if purged_at:
                status_str = f"[bright_red]purged {purged_at[:10]}[/bright_red]"
            elif expires_at:
                status_str = f"expires {expires_at[:16].replace('T', ' ')}"
            else:
                status_str = "[dim]never[/dim]"

            table.add_row(
                f"[link={full_id}]{full_id}[/link]" if state.debug else full_id,
                tool,
                filename,
                cat,
                sc,
                size_str,
                status_str,
            )

        console.print(table)
        return

    # ── download ─────────────────────────────────────────────────────────
    if subcommand == "download":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: files download <id> \\[dest_path][/{COLOR_ERROR}]")
            return

        file_id = args[1]
        dest_path = args[2] if len(args) >= 3 else None

        try:
            with console.status(f"[bold bright_yellow]Downloading {file_id[:8]}…[/bold bright_yellow]", spinner="dots"):
                saved_path = client.download_file(file_id, dest_path)
            console.print(f"[{COLOR_SUCCESS}]✓ Saved to: {saved_path}[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Download failed: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("download_file failed")
        return

    # ── upload ───────────────────────────────────────────────────────────
    if subcommand == "upload":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: files upload <path> \\[--description \"text\"] \\[--scope user|org|dept][/{COLOR_ERROR}]")
            return

        file_path = args[1]
        description: str | None = None
        scope = "user"
        remaining_up = args[2:]
        i = 0
        while i < len(remaining_up):
            if remaining_up[i] in ("--description", "-d") and i + 1 < len(remaining_up):
                description = remaining_up[i + 1]
                i += 2
            elif remaining_up[i] in ("--scope", "-s") and i + 1 < len(remaining_up):
                raw_scope = remaining_up[i + 1].lower()
                if raw_scope in ("org", "organization"):
                    scope = "organization"
                elif raw_scope in ("dept", "department"):
                    scope = "department"
                else:
                    scope = "user"
                i += 2
            else:
                i += 1

        try:
            with console.status(f"[bold bright_yellow]Uploading {file_path}…[/bold bright_yellow]", spinner="dots"):
                result = client.upload_file(file_path, description=description, scope=scope)
            fid = str(result.get("id", ""))
            fname = result.get("filename", file_path)
            fscope = result.get("scope", scope)
            console.print(f"[{COLOR_SUCCESS}]✓ Template uploaded: {fname} (id={fid[:8]}…, scope={fscope})[/{COLOR_SUCCESS}]")
        except FileNotFoundError as exc:
            console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Upload failed: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("upload_file failed")
        return

    # ── delete ───────────────────────────────────────────────────────────
    if subcommand == "delete":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: files delete <id>[/{COLOR_ERROR}]")
            return

        file_id = args[1]
        confirm = console.input(
            f"[{COLOR_INFO}]Delete template {file_id[:8]}…? This cannot be undone. [y/N]: [/{COLOR_INFO}]"
        ).strip().lower()
        if confirm not in ("y", "yes"):
            console.print(f"[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return

        try:
            client.delete_file(file_id)
            console.print(f"[{COLOR_SUCCESS}]✓ Template {file_id[:8]}… deleted.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Delete failed: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("delete_file failed")
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. "
        f"Use: list, download, upload, delete[/{COLOR_ERROR}]"
    )


# ---------------------------------------------------------------------------
# Background tasks commands
# ---------------------------------------------------------------------------


def handle_tasks_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'tasks' subcommands.

    tasks list \\[[page] \\[[limit] \\[[--tool name] \\[[--status status] \\[[--session id]
    tasks show <id>
    """
    if not args:
        console.print(f"[{COLOR_ERROR}]Usage: tasks <list|show>[/{COLOR_ERROR}]")
        return

    subcommand = args[0].lower()

    if subcommand == "list":
        page = 1
        limit = 20
        tool_name: str | None = None
        task_status: str | None = None
        session_id: str | None = None
        remaining = args[1:]

        clean: list[str] = []
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--tool", "-t") and i + 1 < len(remaining):
                tool_name = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--status", "-s") and i + 1 < len(remaining):
                task_status = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--session",) and i + 1 < len(remaining):
                session_id = remaining[i + 1]
                i += 2
            else:
                clean.append(remaining[i])
                i += 1

        try:
            if len(clean) >= 1:
                page = int(clean[0])
            if len(clean) >= 2:
                limit = int(clean[1])
        except ValueError:
            console.print(f"[{COLOR_ERROR}]Usage: tasks list \\[[page] \\[[limit] \\[[--tool name] \\[[--status status][/{COLOR_ERROR}]")
            return

        try:
            data = client.list_background_tasks(
                page=page,
                limit=limit,
                tool_name=tool_name,
                task_status=task_status,
                session_id=session_id,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list tasks: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_background_tasks failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No background tasks found.[/{COLOR_INFO}]")
            return

        def _build_tasks_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"Background Tasks  ({total} total)")
            tbl.add_column("ID", style="bright_cyan", no_wrap=True)
            tbl.add_column("Tool", style="bright_yellow")
            tbl.add_column("Status", justify="center")
            tbl.add_column("Created", style="dim", no_wrap=True)
            tbl.add_column("Error", style="dim")
            for item in items:
                task_id = str(item.get("id", ""))
                tool = item.get("tool_name") or "-"
                st = item.get("status") or "?"
                color = (
                    "bright_green" if st in ("COMPLETED", "completed")
                    else "bright_red" if st in ("FAILED", "failed")
                    else "bright_yellow"
                )
                created = str(item.get("created_at") or "")[:16].replace("T", " ")
                error = (item.get("error_message") or "")[:40]
                tbl.add_row(task_id, tool, f"[{color}]{st}[/{color}]", created, error)
            return tbl

        render_paginated_table(
            console,
            data,
            _build_tasks_table,
            command_hint="tasks list",
            fetch_fn=lambda pg: client.list_background_tasks(
                page=pg, limit=limit,
                tool_name=tool_name, task_status=task_status, session_id=session_id,
            ),
        )
        return

    console.print(f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. Use: list[/{COLOR_ERROR}]")


# ---------------------------------------------------------------------------
# API keys commands
# ---------------------------------------------------------------------------


def handle_api_keys_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'api-keys' subcommands.

    api-keys list \\[[page] \\[[limit]
    """
    if not args:
        console.print(f"[{COLOR_ERROR}]Usage: api-keys <list>[/{COLOR_ERROR}]")
        return

    subcommand = args[0].lower()

    if subcommand == "list":
        page = 1
        limit = 20
        try:
            if len(args) >= 2:
                page = int(args[1])
            if len(args) >= 3:
                limit = int(args[2])
        except ValueError:
            console.print(f"[{COLOR_ERROR}]Usage: api-keys list \\[[page] \\[[limit][/{COLOR_ERROR}]")
            return

        try:
            data = client.list_api_keys(page=page, limit=limit)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list API keys: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_api_keys failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No API keys found.[/{COLOR_INFO}]")
            return

        def _build_keys_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"API Keys  ({total} total)")
            tbl.add_column("Prefix", style="bright_cyan", no_wrap=True)
            tbl.add_column("Name", style="bright_yellow")
            tbl.add_column("Env", style="dim", justify="center")
            tbl.add_column("Interface", style="dim", justify="center")
            tbl.add_column("Status", justify="center")
            tbl.add_column("Expires", style="dim", no_wrap=True)
            for key in items:
                prefix = key.get("key_prefix") or "-"
                name = key.get("name") or "-"
                env = key.get("environment") or "-"
                interface = key.get("interface") or "-"
                is_active = key.get("is_active", True)
                status_str = "[bright_green]active[/bright_green]" if is_active else "[bright_red]revoked[/bright_red]"
                expires = str(key.get("expires_at") or "")[:10]
                tbl.add_row(prefix, name, env, interface, status_str, expires)
            return tbl

        render_paginated_table(
            console,
            data,
            _build_keys_table,
            command_hint="api-keys list",
            fetch_fn=lambda pg: client.list_api_keys(page=pg, limit=limit),
        )
        return

    console.print(f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. Use: list[/{COLOR_ERROR}]")


def _format_bytes(n: int) -> str:
    """Return a human-readable byte count string."""
    value: float = n
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# ---------------------------------------------------------------------------
# AI Agents commands
# ---------------------------------------------------------------------------


def handle_scheduled_jobs_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'agents' subcommands.

    agents list [page] [limit] [--type TYPE] [--active true|false]
    agents show <id>
    agents create --name "..." --cron "..." --prompt "..." [--tz TZ] [--desc "..."]
    agents activate <id>
    agents deactivate <id>
    agents delete <id>
    """
    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: agents <list|show|create|activate|deactivate|delete>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    # ── list ─────────────────────────────────────────────────────────────
    if subcommand == "list":
        page = 1
        limit = 20
        job_type: str | None = None
        is_active: bool | None = None
        remaining = args[1:]

        clean: list[str] = []
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--type", "-t") and i + 1 < len(remaining):
                job_type = remaining[i + 1].upper()
                i += 2
            elif remaining[i] in ("--active", "-a") and i + 1 < len(remaining):
                val = remaining[i + 1].lower()
                is_active = val not in ("false", "0", "no")
                i += 2
            else:
                clean.append(remaining[i])
                i += 1

        try:
            if len(clean) >= 1:
                page = int(clean[0])
            if len(clean) >= 2:
                limit = int(clean[1])
        except ValueError:
            console.print(
                f"[{COLOR_ERROR}]Usage: agents list [page] [limit] [--type PROMPT_RUN|SYSTEM_TASK] [--active true|false][/{COLOR_ERROR}]"
            )
            return

        try:
            data = client.list_scheduled_jobs(
                page=page, limit=limit, job_type=job_type, is_active=is_active
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list AI agents: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_scheduled_jobs failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No AI agents found.[/{COLOR_INFO}]")
            return

        def _build_jobs_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"AI Agents  ({total} total)")
            tbl.add_column("ID", style="bright_cyan", no_wrap=True)
            tbl.add_column("Name", style="bright_yellow")
            tbl.add_column("Type", style="dim", justify="center")
            tbl.add_column("Cron", style="dim")
            tbl.add_column("TZ", style="dim", no_wrap=True)
            tbl.add_column("Active", justify="center")
            tbl.add_column("Runs", justify="right")
            tbl.add_column("Last Status", justify="center")
            for job in items:
                jid = str(job.get("id", ""))
                name = job.get("name") or "-"
                jtype = job.get("job_type") or "-"
                cron = job.get("cron_expression") or "-"
                tz = job.get("timezone") or "UTC"
                active = (
                    "[bright_green]✓[/bright_green]"
                    if job.get("is_active")
                    else "[bright_red]✗[/bright_red]"
                )
                runs = str(job.get("run_count", 0))
                last_st = job.get("last_run_status") or "-"
                tbl.add_row(jid, name, jtype, cron, tz, active, runs, last_st)
            return tbl

        render_paginated_table(
            console,
            data,
            _build_jobs_table,
            command_hint="agents list",
            fetch_fn=lambda pg: client.list_scheduled_jobs(
                page=pg, limit=limit, job_type=job_type, is_active=is_active
            ),
        )
        return

    # ── show ─────────────────────────────────────────────────────────────
    if subcommand == "show":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: agents show <id>[/{COLOR_ERROR}]")
            return
        job_id = args[1]
        try:
            job = client.get_scheduled_job(job_id)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get job: {exc}[/{COLOR_ERROR}]")
            return

        tbl = Table(title=f"AI Agent: {job.get('name', '')}")
        tbl.add_column("Field", style="bright_cyan")
        tbl.add_column("Value")
        fields = [
            ("id", "ID"),
            ("name", "Name"),
            ("description", "Description"),
            ("job_type", "Type"),
            ("cron_expression", "Cron"),
            ("timezone", "Timezone"),
            ("is_active", "Active"),
            ("max_runs", "Max runs"),
            ("run_count", "Run count"),
            ("last_run_at", "Last run"),
            ("last_run_status", "Last status"),
            ("redbeat_key", "RedBeat key"),
            ("created_at", "Created"),
        ]
        for key, label in fields:
            val = job.get(key)
            if val is None:
                continue
            tbl.add_row(label, str(val))
        if job.get("prompt_content"):
            content = str(job["prompt_content"])
            if len(content) > 200:
                content = content[:200] + "…"
            tbl.add_row("Prompt", content)
        console.print(tbl)
        return

    # ── create ────────────────────────────────────────────────────────────
    if subcommand == "create":
        name: str | None = None
        cron: str | None = None
        prompt: str | None = None
        tz = "UTC"
        desc: str | None = None
        max_runs: int | None = None

        remaining = args[1:]
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--name", "-n") and i + 1 < len(remaining):
                name = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--cron", "-c") and i + 1 < len(remaining):
                cron = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--prompt", "-p") and i + 1 < len(remaining):
                prompt = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--tz", "--timezone") and i + 1 < len(remaining):
                tz = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--desc", "--description") and i + 1 < len(remaining):
                desc = remaining[i + 1]
                i += 2
            elif remaining[i] == "--max-runs" and i + 1 < len(remaining):
                try:
                    max_runs = int(remaining[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1

        if not name or not cron or not prompt:
            console.print(
                f"[{COLOR_ERROR}]Usage: agents create --name \"...\" --cron \"...\" --prompt \"...\" [--tz TZ][/{COLOR_ERROR}]"
            )
            return

        try:
            job = client.create_scheduled_job(
                name=name,
                job_type="PROMPT_RUN",
                cron_expression=cron,
                timezone=tz,
                prompt_content=prompt,
                description=desc,
                max_runs=max_runs,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to create job: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("create_scheduled_job failed")
            return

        console.print(
            f"[{COLOR_SUCCESS}]✓ AI agent created: {job.get('id')} ({job.get('name')})[/{COLOR_SUCCESS}]"
        )
        if job.get("redbeat_key"):
            console.print(f"[dim]RedBeat key: {job['redbeat_key']}[/dim]")
        return

    # ── update ────────────────────────────────────────────────────────────
    if subcommand == "update":
        if len(args) < 2:
            console.print(
                f"[{COLOR_ERROR}]Usage: agents update <id> [--name \"...\"] [--cron \"...\"] "
                f"[--prompt \"...\"] [--tz TZ] [--desc \"...\"] [--max-runs N][/{COLOR_ERROR}]"
            )
            return
        job_id = args[1]
        name: str | None = None
        cron: str | None = None
        prompt: str | None = None
        tz: str | None = None
        desc: str | None = None
        max_runs: int | None = None

        remaining = args[2:]
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--name", "-n") and i + 1 < len(remaining):
                name = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--cron", "-c") and i + 1 < len(remaining):
                cron = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--prompt", "-p") and i + 1 < len(remaining):
                prompt = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--tz", "--timezone") and i + 1 < len(remaining):
                tz = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--desc", "--description") and i + 1 < len(remaining):
                desc = remaining[i + 1]
                i += 2
            elif remaining[i] == "--max-runs" and i + 1 < len(remaining):
                try:
                    max_runs = int(remaining[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1

        if not any(v is not None for v in (name, cron, prompt, tz, desc, max_runs)):
            console.print(
                f"[{COLOR_ERROR}]No fields to update. Provide at least one option.[/{COLOR_ERROR}]"
            )
            return

        try:
            job = client.update_scheduled_job(
                job_id=job_id,
                name=name,
                description=desc,
                cron_expression=cron,
                timezone=tz,
                prompt_content=prompt,
                max_runs=max_runs,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to update job: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("update_scheduled_job failed")
            return

        console.print(
            f"[{COLOR_SUCCESS}]✓ AI agent updated: {job.get('name')} ({job.get('id')})[/{COLOR_SUCCESS}]"
        )
        if job.get("redbeat_key"):
            console.print(f"[dim]RedBeat key: {job['redbeat_key']}[/dim]")
        return

    # ── activate ──────────────────────────────────────────────────────────
    if subcommand == "activate":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: agents activate <id>[/{COLOR_ERROR}]")
            return
        try:
            job = client.activate_scheduled_job(args[1])
            console.print(f"[{COLOR_SUCCESS}]✓ Job '{job.get('name')}' activated.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to activate job: {exc}[/{COLOR_ERROR}]")
        return

    # ── deactivate ────────────────────────────────────────────────────────
    if subcommand == "deactivate":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: agents deactivate <id>[/{COLOR_ERROR}]")
            return
        try:
            job = client.deactivate_scheduled_job(args[1])
            console.print(f"[{COLOR_SUCCESS}]✓ Job '{job.get('name')}' deactivated.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to deactivate job: {exc}[/{COLOR_ERROR}]")
        return

    # ── delete ────────────────────────────────────────────────────────────
    if subcommand == "delete":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: agents delete <id>[/{COLOR_ERROR}]")
            return
        job_id = args[1]
        try:
            confirm = input(f"Delete AI agent '{job_id}'? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        if confirm not in ("y", "yes"):
            console.print(f"[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        try:
            client.delete_scheduled_job(job_id)
            console.print(f"[{COLOR_SUCCESS}]✓ AI agent deleted.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to delete job: {exc}[/{COLOR_ERROR}]")
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. Use: list|show|create|update|activate|deactivate|delete[/{COLOR_ERROR}]"
    )


# ---------------------------------------------------------------------------
# Approval Rules commands
# ---------------------------------------------------------------------------


def handle_approval_rules_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'approval-rules' subcommands.

    approval-rules list [page] [limit] [--active true|false] [--tool PATTERN]
    approval-rules show <id>
    approval-rules create --tool PATTERN --approver USER_ID [--user USER_ID|*] [--dept UUID|*] [--priority N] [--desc "..."]
    approval-rules update <id> [--tool PATTERN] [--approver USER_ID] [--user USER_ID|*] [--dept UUID|*] [--priority N] [--desc "..."]
    approval-rules activate <id>
    approval-rules deactivate <id>
    approval-rules delete <id>
    """
    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: approval-rules <list|show|create|update|activate|deactivate|delete>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    # ── list ─────────────────────────────────────────────────────────────
    if subcommand == "list":
        page = 1
        limit = 20
        is_active: bool | None = None
        tool_pattern: str | None = None
        remaining = args[1:]

        clean: list[str] = []
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--active", "-a") and i + 1 < len(remaining):
                val = remaining[i + 1].lower()
                is_active = val not in ("false", "0", "no")
                i += 2
            elif remaining[i] in ("--tool", "-t") and i + 1 < len(remaining):
                tool_pattern = remaining[i + 1]
                i += 2
            else:
                clean.append(remaining[i])
                i += 1

        try:
            if len(clean) >= 1:
                page = int(clean[0])
            if len(clean) >= 2:
                limit = int(clean[1])
        except ValueError:
            console.print(
                f"[{COLOR_ERROR}]Usage: approval-rules list [page] [limit] [--active true|false] [--tool PATTERN][/{COLOR_ERROR}]"
            )
            return

        try:
            data = client.list_approval_rules(
                page=page, limit=limit, is_active=is_active, tool_pattern=tool_pattern
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list approval rules: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_approval_rules failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No approval rules found.[/{COLOR_INFO}]")
            return

        def _build_rules_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"Approval Rules  ({total} total)")
            tbl.add_column("ID", style="bright_cyan", no_wrap=True)
            tbl.add_column("Tool pattern", style="bright_yellow")
            tbl.add_column("Dept pattern", style="dim")
            tbl.add_column("User pattern", style="dim")
            tbl.add_column("Approver", style="dim")
            tbl.add_column("Priority", justify="right", style="dim")
            tbl.add_column("Active", justify="center")
            for rule in items:
                rid = str(rule.get("id", ""))
                tool = rule.get("tool_pattern") or "-"
                dept_pat = rule.get("dept_id_pattern") or "*"
                user_pat = rule.get("user_id_pattern") or "*"
                approver = str(rule.get("approver_user_id") or "-")
                priority = str(rule.get("priority", 0))
                active = (
                    "[bright_green]✓[/bright_green]"
                    if rule.get("is_active")
                    else "[bright_red]✗[/bright_red]"
                )
                tbl.add_row(rid, tool, dept_pat, user_pat, approver, priority, active)
            return tbl

        render_paginated_table(
            console,
            data,
            _build_rules_table,
            command_hint="approval-rules list",
            fetch_fn=lambda pg: client.list_approval_rules(
                page=pg, limit=limit, is_active=is_active, tool_pattern=tool_pattern
            ),
        )
        return

    # ── show ─────────────────────────────────────────────────────────────
    if subcommand == "show":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: approval-rules show <id>[/{COLOR_ERROR}]")
            return
        rule_id = args[1]
        try:
            rule = client.get_approval_rule(rule_id)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get approval rule: {exc}[/{COLOR_ERROR}]")
            return

        tbl = Table(title=f"Approval Rule: {rule.get('id', '')}")
        tbl.add_column("Field", style="bright_cyan")
        tbl.add_column("Value")
        fields = [
            ("id", "ID"),
            ("org_id_pattern", "Org pattern"),
            ("dept_id_pattern", "Dept pattern"),
            ("user_id_pattern", "User pattern"),
            ("tool_pattern", "Tool pattern"),
            ("approver_user_id", "Approver user ID"),
            ("is_active", "Active"),
            ("priority", "Priority"),
            ("description", "Description"),
            ("created_at", "Created"),
            ("updated_at", "Updated"),
        ]
        for key, label in fields:
            val = rule.get(key)
            if val is None:
                continue
            if key == "is_active":
                val = "[bright_green]✓[/bright_green]" if val else "[bright_red]✗[/bright_red]"
            tbl.add_row(label, str(val))
        console.print(tbl)
        return

    # ── create ────────────────────────────────────────────────────────────
    if subcommand == "create":
        tool: str | None = None
        approver: str | None = None
        user_pat = "*"
        dept_pat = "*"
        priority = 0
        desc: str | None = None

        remaining = args[1:]
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--tool", "-t") and i + 1 < len(remaining):
                tool = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--approver", "--approver-id") and i + 1 < len(remaining):
                approver = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--user", "--user-pattern") and i + 1 < len(remaining):
                user_pat = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--dept", "--dept-id") and i + 1 < len(remaining):
                dept_pat = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--priority", "-p") and i + 1 < len(remaining):
                try:
                    priority = int(remaining[i + 1])
                except ValueError:
                    pass
                i += 2
            elif remaining[i] in ("--desc", "--description") and i + 1 < len(remaining):
                desc = remaining[i + 1]
                i += 2
            else:
                i += 1

        if not tool or not approver:
            console.print(
                f"[{COLOR_ERROR}]Usage: approval-rules create --tool PATTERN --approver USER_ID "
                f"[--user USER_ID|*] [--dept UUID|*] [--priority N] [--desc \"...\"][/{COLOR_ERROR}]"
            )
            return

        try:
            rule = client.create_approval_rule(
                tool_pattern=tool,
                approver_user_id=approver,
                user_id_pattern=user_pat,
                dept_id_pattern=dept_pat,
                priority=priority,
                description=desc,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to create approval rule: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("create_approval_rule failed")
            return

        console.print(
            f"[{COLOR_SUCCESS}]✓ Approval rule created: {rule.get('id')}[/{COLOR_SUCCESS}]"
        )
        console.print(
            f"[dim]Tool: {rule.get('tool_pattern')}  Dept: {rule.get('dept_id_pattern')}  "
            f"User: {rule.get('user_id_pattern')}  "
            f"Approver: {rule.get('approver_user_id')}[/dim]"
        )
        return

    # ── update ────────────────────────────────────────────────────────────
    if subcommand == "update":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: approval-rules update <id> [--tool PATTERN] ...[/{COLOR_ERROR}]")
            return
        rule_id = args[1]
        tool_upd: str | None = None
        approver_upd: str | None = None
        user_upd: str | None = None
        dept_upd: str | None = None
        priority_upd: int | None = None
        desc_upd: str | None = None

        remaining = args[2:]
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--tool", "-t") and i + 1 < len(remaining):
                tool_upd = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--approver", "--approver-id") and i + 1 < len(remaining):
                approver_upd = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--user", "--user-pattern") and i + 1 < len(remaining):
                user_upd = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--dept", "--dept-id") and i + 1 < len(remaining):
                dept_upd = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--priority", "-p") and i + 1 < len(remaining):
                try:
                    priority_upd = int(remaining[i + 1])
                except ValueError:
                    pass
                i += 2
            elif remaining[i] in ("--desc", "--description") and i + 1 < len(remaining):
                desc_upd = remaining[i + 1]
                i += 2
            else:
                i += 1

        if not any(x is not None for x in [tool_upd, approver_upd, user_upd, dept_upd, priority_upd, desc_upd]):
            console.print(
                f"[{COLOR_ERROR}]Provide at least one field to update (--tool, --approver, --user, --dept, --priority, --desc)[/{COLOR_ERROR}]"
            )
            return

        try:
            rule = client.update_approval_rule(
                rule_id,
                tool_pattern=tool_upd,
                approver_user_id=approver_upd,
                user_id_pattern=user_upd,
                dept_id_pattern=dept_upd,
                priority=priority_upd,
                description=desc_upd,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to update approval rule: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("update_approval_rule failed")
            return

        console.print(f"[{COLOR_SUCCESS}]✓ Approval rule {rule_id[:8]}… updated.[/{COLOR_SUCCESS}]")
        return

    # ── activate ──────────────────────────────────────────────────────────
    if subcommand == "activate":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: approval-rules activate <id>[/{COLOR_ERROR}]")
            return
        try:
            rule = client.activate_approval_rule(args[1])
            console.print(
                f"[{COLOR_SUCCESS}]✓ Rule '{rule.get('tool_pattern')}' activated.[/{COLOR_SUCCESS}]"
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to activate rule: {exc}[/{COLOR_ERROR}]")
        return

    # ── deactivate ────────────────────────────────────────────────────────
    if subcommand == "deactivate":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: approval-rules deactivate <id>[/{COLOR_ERROR}]")
            return
        try:
            rule = client.deactivate_approval_rule(args[1])
            console.print(
                f"[{COLOR_SUCCESS}]✓ Rule '{rule.get('tool_pattern')}' deactivated.[/{COLOR_SUCCESS}]"
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to deactivate rule: {exc}[/{COLOR_ERROR}]")
        return

    # ── delete ────────────────────────────────────────────────────────────
    if subcommand == "delete":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: approval-rules delete <id>[/{COLOR_ERROR}]")
            return
        rule_id = args[1]
        try:
            confirm = input(f"Delete approval rule '{rule_id}'? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        if confirm not in ("y", "yes"):
            console.print(f"[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        try:
            client.delete_approval_rule(rule_id)
            console.print(f"[{COLOR_SUCCESS}]✓ Approval rule deleted.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to delete rule: {exc}[/{COLOR_ERROR}]")
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. "
        f"Use: list|show|create|update|activate|deactivate|delete[/{COLOR_ERROR}]"
    )


# ---------------------------------------------------------------------------
# DataStores commands
# ---------------------------------------------------------------------------


def handle_datastores_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'datastores' subcommands.

    datastores list [page] [limit]
    datastores show <store_id>
    datastores create --name "..." [--desc "..."] [--visibility shared|private] [--max-records N] [--schema '{}']
    datastores update <store_id> [--name "..."] [--desc "..."] [--visibility shared|private] [--max-records N] [--activate|--deactivate]
    datastores delete <store_id>
    datastores records <store_id> [page] [limit]
    datastores record <store_id> <record_id>
    datastores add-record <store_id> <json_data>
    datastores update-record <store_id> <record_id> <json_data>
    datastores delete-record <store_id> <record_id>
    datastores query <store_id> [json_filters]
    """
    import json as _json

    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: datastores <list|show|create|update|delete|records|record|add-record|update-record|delete-record|query>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    # ── list ──────────────────────────────────────────────────────────────
    if subcommand == "list":
        page = 1
        limit = 20
        try:
            if len(args) >= 2:
                page = int(args[1])
            if len(args) >= 3:
                limit = int(args[2])
        except ValueError:
            console.print(f"[{COLOR_ERROR}]Usage: datastores list [page] [limit][/{COLOR_ERROR}]")
            return

        try:
            data = client.list_datastores(page=page, limit=limit)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list datastores: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_datastores failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No data stores found.[/{COLOR_INFO}]")
            return

        def _build_stores_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"DataStores  ({total} total)")
            tbl.add_column("ID", style="bright_cyan", no_wrap=True)
            tbl.add_column("Name", style="bright_yellow")
            tbl.add_column("Visibility", style="dim")
            tbl.add_column("Records", justify="right")
            tbl.add_column("Max", justify="right", style="dim")
            tbl.add_column("Active", justify="center")
            for s in items:
                sid = str(s.get("id", ""))
                name = s.get("name") or "-"
                vis = s.get("visibility") or "-"
                count = str(s.get("record_count", 0))
                max_r = str(s.get("max_records", 0)) if s.get("max_records") else "∞"
                active = (
                    "[bright_green]✓[/bright_green]"
                    if s.get("is_active")
                    else "[bright_red]✗[/bright_red]"
                )
                tbl.add_row(sid, name, vis, count, max_r, active)
            return tbl

        render_paginated_table(
            console,
            data,
            _build_stores_table,
            command_hint="datastores list",
            fetch_fn=lambda pg: client.list_datastores(page=pg, limit=limit),
        )
        return

    # ── show ──────────────────────────────────────────────────────────────
    if subcommand == "show":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: datastores show <store_id>[/{COLOR_ERROR}]")
            return
        try:
            store = client.get_datastore(args[1])
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get datastore: {exc}[/{COLOR_ERROR}]")
            return

        tbl = Table(title=f"DataStore: {str(store.get('id', ''))}")
        tbl.add_column("Field", style="bright_cyan")
        tbl.add_column("Value")
        for key, label in [
            ("id", "ID"),
            ("name", "Name"),
            ("description", "Description"),
            ("visibility", "Visibility"),
            ("record_count", "Record count"),
            ("max_records", "Max records"),
            ("is_active", "Active"),
            ("created_by", "Created by"),
            ("created_at", "Created"),
            ("updated_at", "Updated"),
        ]:
            val = store.get(key)
            if val is None:
                continue
            if key == "is_active":
                val = "[bright_green]✓[/bright_green]" if val else "[bright_red]✗[/bright_red]"
            tbl.add_row(label, str(val))
        console.print(tbl)

        schema = store.get("schema")
        if schema:
            console.print(f"[dim]Schema:[/dim] {_json.dumps(schema, indent=2)}")
        return

    # ── create ────────────────────────────────────────────────────────────
    if subcommand == "create":
        name: str | None = None
        desc: str | None = None
        visibility = "shared"
        max_records: int | None = None
        schema: dict | None = None

        remaining = args[1:]
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--name", "-n") and i + 1 < len(remaining):
                name = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--desc", "--description") and i + 1 < len(remaining):
                desc = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--visibility", "--vis") and i + 1 < len(remaining):
                visibility = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--max-records", "--max") and i + 1 < len(remaining):
                try:
                    max_records = int(remaining[i + 1])
                except ValueError:
                    pass
                i += 2
            elif remaining[i] == "--schema" and i + 1 < len(remaining):
                try:
                    schema = _json.loads(remaining[i + 1])
                except _json.JSONDecodeError:
                    console.print(f"[{COLOR_ERROR}]Invalid JSON for --schema[/{COLOR_ERROR}]")
                    return
                i += 2
            else:
                i += 1

        if not name:
            console.print(
                f"[{COLOR_ERROR}]Usage: datastores create --name \"...\" "
                f"[--desc \"...\"] [--visibility shared|private] "
                f"[--max-records N] [--schema '{{}}'][/{COLOR_ERROR}]"
            )
            return

        try:
            store = client.create_datastore(
                name=name,
                description=desc,
                schema=schema,
                visibility=visibility,
                max_records=max_records,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to create datastore: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("create_datastore failed")
            return

        console.print(f"[{COLOR_SUCCESS}]✓ DataStore created: {store.get('id')}[/{COLOR_SUCCESS}]")
        console.print(f"[dim]Name: {store.get('name')}  Visibility: {store.get('visibility')}[/dim]")
        return

    # ── update ────────────────────────────────────────────────────────────
    if subcommand == "update":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: datastores update <store_id> [--name ...][/{COLOR_ERROR}]")
            return
        store_id = args[1]
        name_upd: str | None = None
        desc_upd: str | None = None
        vis_upd: str | None = None
        max_upd: int | None = None
        active_upd: bool | None = None

        remaining = args[2:]
        i = 0
        while i < len(remaining):
            if remaining[i] in ("--name", "-n") and i + 1 < len(remaining):
                name_upd = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--desc", "--description") and i + 1 < len(remaining):
                desc_upd = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--visibility", "--vis") and i + 1 < len(remaining):
                vis_upd = remaining[i + 1]
                i += 2
            elif remaining[i] in ("--max-records", "--max") and i + 1 < len(remaining):
                try:
                    max_upd = int(remaining[i + 1])
                except ValueError:
                    pass
                i += 2
            elif remaining[i] == "--activate":
                active_upd = True
                i += 1
            elif remaining[i] == "--deactivate":
                active_upd = False
                i += 1
            else:
                i += 1

        try:
            store = client.update_datastore(
                store_id=store_id,
                name=name_upd,
                description=desc_upd,
                visibility=vis_upd,
                max_records=max_upd,
                is_active=active_upd,
            )
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to update datastore: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("update_datastore failed")
            return

        console.print(f"[{COLOR_SUCCESS}]✓ DataStore updated: {store.get('name')}[/{COLOR_SUCCESS}]")
        return

    # ── delete ────────────────────────────────────────────────────────────
    if subcommand == "delete":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: datastores delete <store_id>[/{COLOR_ERROR}]")
            return
        store_id = args[1]
        try:
            confirm = input(f"Delete data store '{store_id}' and all its records? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        if confirm not in ("y", "yes"):
            console.print(f"[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        try:
            client.delete_datastore(store_id)
            console.print(f"[{COLOR_SUCCESS}]✓ DataStore deleted.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to delete datastore: {exc}[/{COLOR_ERROR}]")
        return

    # ── records ───────────────────────────────────────────────────────────
    if subcommand == "records":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: datastores records <store_id> [page] [limit][/{COLOR_ERROR}]")
            return
        store_id = args[1]
        page = 1
        limit = 20
        try:
            if len(args) >= 3:
                page = int(args[2])
            if len(args) >= 4:
                limit = int(args[3])
        except ValueError:
            pass

        try:
            data = client.list_datastore_records(store_id, page=page, limit=limit)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list records: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("list_datastore_records failed")
            return

        items = data.get("items", [])
        if not items:
            console.print(f"[{COLOR_INFO}]No records found.[/{COLOR_INFO}]")
            return

        def _build_records_table(items: list, data: dict) -> Table:
            total = data.get("total", len(items))
            tbl = Table(title=f"Records in {store_id} ({total} total)")
            tbl.add_column("ID", style="bright_cyan", no_wrap=True)
            tbl.add_column("Data", style="dim", max_width=60)
            tbl.add_column("Created", style="dim")
            for r in items:
                rid = str(r.get("id", ""))
                data_str = _json.dumps(r.get("data", {}))
                if len(data_str) > 58:
                    data_str = data_str[:55] + "…"
                created = str(r.get("created_at", ""))[:19]
                tbl.add_row(rid, data_str, created)
            return tbl

        render_paginated_table(
            console,
            data,
            _build_records_table,
            command_hint=f"datastores records {store_id}",
            fetch_fn=lambda pg: client.list_datastore_records(store_id, page=pg, limit=limit),
        )
        return

    # ── record ────────────────────────────────────────────────────────────
    if subcommand == "record":
        if len(args) < 3:
            console.print(f"[{COLOR_ERROR}]Usage: datastores record <store_id> <record_id>[/{COLOR_ERROR}]")
            return
        try:
            rec = client.get_datastore_record(args[1], args[2])
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get record: {exc}[/{COLOR_ERROR}]")
            return
        console.print(f"[bright_cyan]ID:[/bright_cyan] {rec.get('id')}")
        console.print(f"[bright_cyan]Store:[/bright_cyan] {rec.get('datastore_id')}")
        console.print(f"[bright_cyan]Data:[/bright_cyan]")
        console.print(_json.dumps(rec.get("data", {}), indent=2))
        console.print(f"[dim]Created: {rec.get('created_at')}  Updated: {rec.get('updated_at')}[/dim]")
        return

    # ── add-record ────────────────────────────────────────────────────────
    if subcommand == "add-record":
        if len(args) < 3:
            console.print(f"[{COLOR_ERROR}]Usage: datastores add-record <store_id> <json_data>[/{COLOR_ERROR}]")
            return
        try:
            data_obj = _json.loads(args[2])
        except _json.JSONDecodeError as exc:
            console.print(f"[{COLOR_ERROR}]Invalid JSON: {exc}[/{COLOR_ERROR}]")
            return
        try:
            rec = client.insert_datastore_record(args[1], data_obj)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to add record: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("insert_datastore_record failed")
            return
        console.print(f"[{COLOR_SUCCESS}]✓ Record added: {rec.get('id')}[/{COLOR_SUCCESS}]")
        return

    # ── update-record ─────────────────────────────────────────────────────
    if subcommand == "update-record":
        if len(args) < 4:
            console.print(
                f"[{COLOR_ERROR}]Usage: datastores update-record <store_id> <record_id> <json_data>[/{COLOR_ERROR}]"
            )
            return
        try:
            data_obj = _json.loads(args[3])
        except _json.JSONDecodeError as exc:
            console.print(f"[{COLOR_ERROR}]Invalid JSON: {exc}[/{COLOR_ERROR}]")
            return
        try:
            rec = client.update_datastore_record(args[1], args[2], data_obj)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to update record: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("update_datastore_record failed")
            return
        console.print(f"[{COLOR_SUCCESS}]✓ Record updated: {rec.get('id')}[/{COLOR_SUCCESS}]")
        return

    # ── delete-record ─────────────────────────────────────────────────────
    if subcommand == "delete-record":
        if len(args) < 3:
            console.print(
                f"[{COLOR_ERROR}]Usage: datastores delete-record <store_id> <record_id>[/{COLOR_ERROR}]"
            )
            return
        try:
            confirm = input(f"Delete record '{args[2]}'? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        if confirm not in ("y", "yes"):
            console.print(f"[{COLOR_INFO}]Cancelled.[/{COLOR_INFO}]")
            return
        try:
            client.delete_datastore_record(args[1], args[2])
            console.print(f"[{COLOR_SUCCESS}]✓ Record deleted.[/{COLOR_SUCCESS}]")
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to delete record: {exc}[/{COLOR_ERROR}]")
        return

    # ── query ─────────────────────────────────────────────────────────────
    if subcommand == "query":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: datastores query <store_id> [json_filters][/{COLOR_ERROR}]")
            return
        store_id = args[1]
        filters: dict | None = None
        if len(args) >= 3:
            try:
                filters = _json.loads(args[2])
            except _json.JSONDecodeError as exc:
                console.print(f"[{COLOR_ERROR}]Invalid JSON filters: {exc}[/{COLOR_ERROR}]")
                return

        try:
            data = client.query_datastore_records(store_id, filters=filters)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to query records: {exc}[/{COLOR_ERROR}]")
            if state.debug:
                logger.exception("query_datastore_records failed")
            return

        items = data.get("items", [])
        total = data.get("total", len(items))
        console.print(f"[{COLOR_INFO}]{total} record(s) found.[/{COLOR_INFO}]")
        for rec in items:
            console.print(
                f"[bright_cyan]{str(rec.get('id', ''))}[/bright_cyan]  "
                + _json.dumps(rec.get("data", {}))
            )
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. "
        f"Use: list|show|create|update|delete|records|record|add-record|update-record|delete-record|query[/{COLOR_ERROR}]"
    )


def handle_dept_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'dept' subcommands.

    dept list          — List all departments in the current org
    dept my            — List your department memberships
    dept info          — Show the currently active department
    dept set <id|slug> — Switch active department by ID or slug
    """
    if not args:
        console.print(
            f"[{COLOR_ERROR}]Usage: dept <list|my|info|set <id|slug>>[/{COLOR_ERROR}]"
        )
        return

    subcommand = args[0].lower()

    # ── list ──────────────────────────────────────────────────────────────
    if subcommand == "list":
        try:
            depts = client.list_departments()
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list departments: {exc}[/{COLOR_ERROR}]")
            return

        if not depts:
            console.print(f"[{COLOR_INFO}]No departments found.[/{COLOR_INFO}]")
            return

        tbl = Table(title="Departments")
        tbl.add_column("ID", style="bright_cyan", no_wrap=True)
        tbl.add_column("Name", style="bright_yellow")
        tbl.add_column("Slug", style="dim")
        tbl.add_column("Default", justify="center")
        tbl.add_column("Active", justify="center")
        for dept in depts:
            did = str(dept.get("id", ""))
            name = dept.get("name") or "-"
            slug = dept.get("slug") or "-"
            is_default = "[bright_green]✓[/bright_green]" if dept.get("is_default") else ""
            is_active = (
                "[bright_green]✓[/bright_green]"
                if dept.get("is_active")
                else "[bright_red]✗[/bright_red]"
            )
            tbl.add_row(did, name, slug, is_default, is_active)
        console.print(tbl)
        return

    # ── my ────────────────────────────────────────────────────────────────
    if subcommand == "my":
        try:
            memberships = client.my_departments()
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list your departments: {exc}[/{COLOR_ERROR}]")
            return

        if not memberships:
            console.print(f"[{COLOR_INFO}]You are not a member of any department.[/{COLOR_INFO}]")
            return

        tbl = Table(title="Your Departments")
        tbl.add_column("Dept ID", style="bright_cyan", no_wrap=True)
        tbl.add_column("Role", style="bright_yellow")
        tbl.add_column("Active", justify="center")
        for m in memberships:
            tbl.add_row(
                str(m.get("dept_id", "")),
                m.get("role") or "-",
                "[bright_green]✓[/bright_green]" if m.get("is_active") else "[bright_red]✗[/bright_red]",
            )
        console.print(tbl)
        return

    # ── info ──────────────────────────────────────────────────────────────
    if subcommand == "info":
        if not client.dept_id:
            console.print(f"[{COLOR_INFO}]No department currently selected.[/{COLOR_INFO}]")
            return
        try:
            dept = client.get_department(client.dept_id)
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to get department info: {exc}[/{COLOR_ERROR}]")
            return

        tbl = Table(title="Active Department")
        tbl.add_column("Field", style="bright_cyan")
        tbl.add_column("Value")
        for key, label in [
            ("id", "ID"),
            ("name", "Name"),
            ("slug", "Slug"),
            ("description", "Description"),
            ("is_default", "Default"),
            ("is_active", "Active"),
        ]:
            val = dept.get(key)
            tbl.add_row(label, str(val) if val is not None else "-")
        console.print(tbl)
        return

    # ── set ──────────────────────────────────────────────────────────────
    if subcommand == "set":
        if len(args) < 2:
            console.print(f"[{COLOR_ERROR}]Usage: dept set <dept_id>[/{COLOR_ERROR}]")
            return

        target = args[1]
        # Try to find by slug or exact ID
        try:
            depts = client.list_departments()
        except Exception as exc:
            console.print(f"[{COLOR_ERROR}]Failed to list departments: {exc}[/{COLOR_ERROR}]")
            return

        match = None
        for d in depts:
            if str(d.get("id", "")) == target or d.get("slug") == target:
                match = d
                break

        if match is None:
            console.print(f"[{COLOR_ERROR}]Department '{target}' not found.[/{COLOR_ERROR}]")
            return

        client.dept_id = str(match["id"])
        console.print(
            f"[{COLOR_SUCCESS}]Active department set to: {match.get('name')} ({match.get('slug')})[/{COLOR_SUCCESS}]"
        )
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown subcommand '{subcommand}'. Use: list|my|info|set[/{COLOR_ERROR}]"
    )


# ---------------------------------------------------------------------------
# handle_admin_command
# ---------------------------------------------------------------------------

def handle_admin_command(
    args: list[str],
    state: "REPLState",
    client: "GSageAPIClient",
    console: Console,
) -> None:
    """Handle 'admin <resource> <subcommand> [args]' commands.

    Requires admin:access permission on the org.
    """
    import argparse as _ap  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    if not args:
        console.print(
            f"[{COLOR_INFO}]Usage: admin <org|users|groups|tool-configs|interfaces|emails> <subcommand>[/{COLOR_INFO}]"
        )
        return

    resource = args[0].lower()
    subargs = args[1:]

    # ── org ──────────────────────────────────────────────────────────────
    if resource == "org":
        subcmd = subargs[0].lower() if subargs else "show"

        if subcmd in ("show", "get"):
            try:
                org = client.admin_get_org()
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=False, box=None, padding=(0, 1))
            tbl.add_column("Field", style="bright_cyan", no_wrap=True)
            tbl.add_column("Value")
            for k, label in [
                ("name", "Name"), ("slug", "Slug"),
                ("llm_provider", "LLM Provider"), ("maker_model", "Maker Model"),
                ("reviewer_model", "Reviewer Model"), ("agent_timeout_seconds", "Timeout (s)"),
                ("max_context_tokens", "Max Tokens"),
            ]:
                tbl.add_row(label, str(org.get(k) or "-"))
            console.print(tbl)
            return

        if subcmd == "update":
            parser = _ap.ArgumentParser(prog="admin org update", add_help=False)
            parser.add_argument("--name"); parser.add_argument("--slug")
            parser.add_argument("--llm-provider"); parser.add_argument("--llm-api-key")
            parser.add_argument("--maker-model"); parser.add_argument("--reviewer-model")
            parser.add_argument("--timeout", type=int); parser.add_argument("--max-tokens", type=int)
            try:
                ns, _ = parser.parse_known_args(subargs[1:])
            except SystemExit:
                return
            payload: dict = {}
            if ns.name: payload["name"] = ns.name
            if ns.slug: payload["slug"] = ns.slug
            if getattr(ns, "llm_provider", None): payload["llm_provider"] = ns.llm_provider
            if getattr(ns, "llm_api_key", None): payload["llm_api_key"] = ns.llm_api_key
            if getattr(ns, "maker_model", None): payload["maker_model"] = ns.maker_model
            if getattr(ns, "reviewer_model", None): payload["reviewer_model"] = ns.reviewer_model
            if ns.timeout: payload["agent_timeout_seconds"] = ns.timeout
            if getattr(ns, "max_tokens", None): payload["max_context_tokens"] = ns.max_tokens
            if not payload:
                console.print(f"[{COLOR_ERROR}]No fields provided.[/{COLOR_ERROR}]")
                return
            try:
                client.admin_update_org(**payload)
                console.print(f"[{COLOR_SUCCESS}]Organization updated.[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        console.print(f"[{COLOR_ERROR}]Unknown subcommand. Use: show|update[/{COLOR_ERROR}]")
        return

    # ── users ─────────────────────────────────────────────────────────────
    if resource == "users":
        subcmd = subargs[0].lower() if subargs else "list"

        if subcmd == "list":
            try:
                page = int(subargs[1]) if len(subargs) > 1 else 1
                limit = int(subargs[2]) if len(subargs) > 2 else 20
                search = None
                if "--search" in subargs:
                    idx = subargs.index("--search")
                    if idx + 1 < len(subargs):
                        search = subargs[idx + 1]
                data = client.admin_list_users(page=page, limit=limit, search=search)
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=True, header_style=f"bold {COLOR_INFO}")
            tbl.add_column("ID", style="dim", no_wrap=True)
            tbl.add_column("Email"); tbl.add_column("Name"); tbl.add_column("Role"); tbl.add_column("Active")
            for u in data.get("items", []):
                tbl.add_row(
                    str(u.get("user_id") or u.get("id", "-")),
                    u.get("email", "-"), u.get("full_name", "-"),
                    u.get("role", "-"), "✓" if u.get("is_active") else "✗",
                )
            console.print(tbl)
            return

        if subcmd == "show" and len(subargs) > 1:
            try:
                user = client.admin_get_user(subargs[1])
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=False, box=None, padding=(0, 1))
            tbl.add_column("Field", style="bright_cyan"); tbl.add_column("Value")
            for k, lbl in [("user_id", "User ID"), ("email", "Email"), ("full_name", "Name"), ("role", "Role"), ("is_active", "Active")]:
                tbl.add_row(lbl, str(user.get(k, "-")))
            console.print(tbl)
            return

        if subcmd == "create":
            parser = _ap.ArgumentParser(prog="admin users create", add_help=False)
            parser.add_argument("--email", required=True)
            parser.add_argument("--name", required=True)
            parser.add_argument("--role", default="user")
            try:
                ns, _ = parser.parse_known_args(subargs[1:])
            except SystemExit:
                return
            try:
                result = client.admin_create_user(email=ns.email, full_name=ns.name, role=ns.role)
                console.print(f"[{COLOR_SUCCESS}]User created: {result.get('email')} (id: {result.get('user_id') or result.get('id')})[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "reset-password" and len(subargs) > 1:
            try:
                result = client.admin_reset_user_password(subargs[1])
                pwd = result.get("temporary_password") or result.get("password") or "-"
                console.print(f"[{COLOR_SUCCESS}]Temporary password: [bold]{pwd}[/bold][/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "reset-otp" and len(subargs) > 1:
            try:
                client.admin_reset_user_otp(subargs[1])
                console.print(f"[{COLOR_SUCCESS}]OTP disabled for user {subargs[1]}.[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "remove" and len(subargs) > 1:
            try:
                confirm = input(f"Remove user {subargs[1]}? [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]")
                return
            try:
                client.admin_remove_user(subargs[1])
                console.print(f"[{COLOR_SUCCESS}]User removed.[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        console.print(f"[{COLOR_ERROR}]Unknown subcommand. Use: list|show|create|reset-password|reset-otp|remove[/{COLOR_ERROR}]")
        return

    # ── groups ────────────────────────────────────────────────────────────
    if resource == "groups":
        subcmd = subargs[0].lower() if subargs else "list"

        if subcmd == "list":
            try:
                groups = client.admin_list_groups()
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=True, header_style=f"bold {COLOR_INFO}")
            tbl.add_column("ID", style="dim"); tbl.add_column("Name"); tbl.add_column("Description")
            for g in groups:
                tbl.add_row(str(g.get("id", "-")), g.get("name", "-"), g.get("description") or "-")
            console.print(tbl)
            return

        if subcmd == "show" and len(subargs) > 1:
            try:
                g = client.admin_get_group(subargs[1])
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            console.print(f"[bold]Group:[/bold] {g.get('name')} (id: {g.get('id')})")
            members = g.get("users", [])
            console.print(f"[bold]Members ({len(members)}):[/bold] " + ", ".join(m.get("email", "-") for m in members))
            perms = g.get("permissions", [])
            console.print(f"[bold]Permissions ({len(perms)}):[/bold] " + ", ".join(p.get("name", "-") for p in perms))
            return

        if subcmd == "create":
            parser = _ap.ArgumentParser(prog="admin groups create", add_help=False)
            parser.add_argument("--name", required=True); parser.add_argument("--desc")
            try:
                ns, _ = parser.parse_known_args(subargs[1:])
            except SystemExit:
                return
            try:
                result = client.admin_create_group(name=ns.name, description=ns.desc)
                console.print(f"[{COLOR_SUCCESS}]Group created: {result.get('name')} (id: {result.get('id')})[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "delete" and len(subargs) > 1:
            try:
                confirm = input(f"Delete group {subargs[1]}? [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]")
                return
            try:
                client.admin_delete_group(subargs[1])
                console.print(f"[{COLOR_SUCCESS}]Group deleted.[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "permissions":
            try:
                perms = client.admin_list_permissions()
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=True, header_style=f"bold {COLOR_INFO}")
            tbl.add_column("Name"); tbl.add_column("Display Name"); tbl.add_column("Category")
            for p in perms:
                tbl.add_row(p.get("name", "-"), p.get("display_name", "-"), p.get("category", "-"))
            console.print(tbl)
            return

        console.print(f"[{COLOR_ERROR}]Unknown subcommand. Use: list|show|create|delete|permissions[/{COLOR_ERROR}]")
        return

    # ── tool-configs ──────────────────────────────────────────────────────
    if resource == "tool-configs":
        subcmd = subargs[0].lower() if subargs else "list"

        if subcmd == "list":
            try:
                configs = client.admin_list_tool_configs()
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=True, header_style=f"bold {COLOR_INFO}")
            tbl.add_column("ID", style="dim"); tbl.add_column("Tool"); tbl.add_column("Profile ID"); tbl.add_column("Description")
            for c in configs:
                tbl.add_row(str(c.get("id", "-")), c.get("tool_name", "-"), c.get("profile_id", "-"), c.get("description") or "-")
            console.print(tbl)
            return

        if subcmd == "create":
            parser = _ap.ArgumentParser(prog="admin tool-configs create", add_help=False)
            parser.add_argument("--tool", required=True); parser.add_argument("--profile", required=True)
            parser.add_argument("--config", default="{}"); parser.add_argument("--desc")
            try:
                ns, _ = parser.parse_known_args(subargs[1:])
            except SystemExit:
                return
            try:
                config_data = _json.loads(ns.config)
            except _json.JSONDecodeError:
                console.print(f"[{COLOR_ERROR}]Invalid JSON for --config[/{COLOR_ERROR}]")
                return
            try:
                result = client.admin_create_tool_config(
                    tool_name=ns.tool, profile_id=ns.profile, config=config_data, description=ns.desc
                )
                console.print(f"[{COLOR_SUCCESS}]Tool config created (id: {result.get('id')})[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "delete" and len(subargs) > 1:
            try:
                confirm = input(f"Delete tool config {subargs[1]}? [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]")
                return
            try:
                client.admin_delete_tool_config(subargs[1])
                console.print(f"[{COLOR_SUCCESS}]Tool config deleted.[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        console.print(f"[{COLOR_ERROR}]Unknown subcommand. Use: list|create|delete[/{COLOR_ERROR}]")
        return

    # ── interfaces ────────────────────────────────────────────────────────
    if resource == "interfaces":
        subcmd = subargs[0].lower() if subargs else "list"

        if subcmd == "list":
            try:
                profiles = client.admin_list_interfaces()
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=True, header_style=f"bold {COLOR_INFO}")
            tbl.add_column("ID", style="dim"); tbl.add_column("Interface"); tbl.add_column("Mode"); tbl.add_column("Active")
            for p in profiles:
                tbl.add_row(str(p.get("id", "-")), p.get("interface", "-"), p.get("mode", "-"), "✓" if p.get("is_active") else "✗")
            console.print(tbl)
            return

        if subcmd == "create":
            parser = _ap.ArgumentParser(prog="admin interfaces create", add_help=False)
            parser.add_argument("--interface", required=True)
            parser.add_argument("--mode", default="denylist", choices=["allowlist", "denylist"])
            parser.add_argument("--tags", default=""); parser.add_argument("--desc")
            try:
                ns, _ = parser.parse_known_args(subargs[1:])
            except SystemExit:
                return
            tags = [t.strip() for t in ns.tags.split(",") if t.strip()] if ns.tags else []
            try:
                result = client.admin_create_interface(
                    interface=ns.interface, mode=ns.mode, tool_permissions=tags, description=ns.desc
                )
                console.print(f"[{COLOR_SUCCESS}]Interface profile created (id: {result.get('id')})[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "delete" and len(subargs) > 1:
            try:
                confirm = input(f"Delete interface profile {subargs[1]}? [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]"); return
            try:
                client.admin_delete_interface(subargs[1])
                console.print(f"[{COLOR_SUCCESS}]Interface profile deleted.[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        console.print(f"[{COLOR_ERROR}]Unknown subcommand. Use: list|create|delete[/{COLOR_ERROR}]")
        return

    # ── emails ────────────────────────────────────────────────────────────
    if resource == "emails":
        subcmd = subargs[0].lower() if subargs else "list"

        if subcmd == "list":
            try:
                accounts = client.admin_list_email_accounts()
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            tbl = Table(show_header=True, header_style=f"bold {COLOR_INFO}")
            tbl.add_column("ID", style="dim"); tbl.add_column("Name"); tbl.add_column("Email"); tbl.add_column("IMAP"); tbl.add_column("Active")
            for a in accounts:
                tbl.add_row(
                    str(a.get("id", "-")), a.get("display_name", "-"), a.get("email", "-"),
                    f"{a.get('imap_host', '-')}:{a.get('imap_port', '-')}",
                    "✓" if a.get("is_active") else "✗",
                )
            console.print(tbl)
            return

        if subcmd == "create":
            parser = _ap.ArgumentParser(prog="admin emails create", add_help=False)
            parser.add_argument("--email", required=True)
            parser.add_argument("--name"); parser.add_argument("--imap-host", required=True)
            parser.add_argument("--imap-port", type=int, default=993)
            parser.add_argument("--smtp-host", required=True)
            parser.add_argument("--smtp-port", type=int, default=465)
            try:
                ns, _ = parser.parse_known_args(subargs[1:])
            except SystemExit:
                return
            try:
                imap_password = getpass.getpass("IMAP password: ")
                smtp_password = getpass.getpass("SMTP password: ")
            except (KeyboardInterrupt, EOFError):
                console.print("[dim]Cancelled.[/dim]"); return
            fields = {
                "email": ns.email,
                "display_name": ns.name or ns.email,
                "imap_host": ns.imap_host, "imap_port": ns.imap_port,
                "imap_password": imap_password,
                "smtp_host": ns.smtp_host, "smtp_port": ns.smtp_port,
                "smtp_password": smtp_password,
                "is_active": True,
            }
            try:
                result = client.admin_create_email_account(**fields)
                console.print(f"[{COLOR_SUCCESS}]Email account created (id: {result.get('id')})[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        if subcmd == "test" and len(subargs) > 1:
            try:
                result = client.admin_test_email_account(subargs[1])
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
                return
            imap_ok = result.get("imap_ok", False)
            smtp_ok = result.get("smtp_ok", False)
            imap_err = result.get("imap_error") or ""
            smtp_err = result.get("smtp_error") or ""
            console.print(
                f"IMAP: {'[green]OK[/green]' if imap_ok else '[red]FAIL[/red]'}{' — ' + imap_err if imap_err else ''}"
            )
            console.print(
                f"SMTP: {'[green]OK[/green]' if smtp_ok else '[red]FAIL[/red]'}{' — ' + smtp_err if smtp_err else ''}"
            )
            return

        if subcmd == "delete" and len(subargs) > 1:
            try:
                confirm = input(f"Delete email account {subargs[1]}? [y/N] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]"); return
            try:
                client.admin_delete_email_account(subargs[1])
                console.print(f"[{COLOR_SUCCESS}]Email account deleted.[/{COLOR_SUCCESS}]")
            except Exception as exc:
                console.print(f"[{COLOR_ERROR}]{exc}[/{COLOR_ERROR}]")
            return

        console.print(f"[{COLOR_ERROR}]Unknown subcommand. Use: list|create|test|delete[/{COLOR_ERROR}]")
        return

    console.print(
        f"[{COLOR_ERROR}]Unknown admin resource '{resource}'. Use: org|users|groups|tool-configs|interfaces|emails[/{COLOR_ERROR}]"
    )
