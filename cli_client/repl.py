"""Interactive REPL for gSage AI CLI."""

from __future__ import annotations

import glob
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

# Import readline for command history and arrow key navigation
# On Windows, this may require pyreadline3 (pip install pyreadline3)
try:
    import readline
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

from cli_client.client import GSageAPIClient
from cli_client.commands import (
    CMD_PERMISSIONS,
    COLOR_ERROR,
    COLOR_USER,
    handle_admin_command,
    handle_approval_rules_command,
    handle_approvals_command,
    handle_datastores_command,
    handle_dept_command,
    handle_files_command,
    handle_api_keys_command,
    handle_conversation_command,
    handle_knowledge_command,
    handle_login_command,
    handle_messages_command,
    handle_otp_command,
    handle_profile_command,
    handle_register_command,
    handle_scheduled_jobs_command,
    handle_send_message,
    handle_tasks_command,
    handle_whoami_command,
    show_help,
    toggle_debug,
)
from cli_client.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tab completion
# ---------------------------------------------------------------------------

# Mapa estático de comando → subcomandos/opções válidas
_COMPLETIONS: dict[str, list[str]] = {
    "help": [],
    "login": [],
    "register": [],
    "whoami": [],
    "profile": ["update", "change-password"],
    "debug": [],
    "clear": [],
    "exit": [],
    "quit": [],
    "messages": [],
    "conversation": ["list", "new", "show", "archive"],
    "approvals": ["list", "show", "approve", "reject"],
    "files": ["list", "download", "upload", "delete"],
    "knowledge": ["search", "list", "add", "delete", "ingest", "status"],
    "tasks": ["list"],
    "api-keys": ["list"],
    "agents": ["list", "show", "create", "update", "activate", "deactivate", "delete"],
    "approval-rules": ["list", "show", "create", "update", "activate", "deactivate", "delete"],
    "datastores": ["list", "show", "create", "update", "delete", "records", "record", "add-record", "update-record", "delete-record", "query"],
    "dept": ["list", "set", "info", "my"],
    "otp": ["status", "enable", "disable", "backup-codes"],
    "editor": [],
    "attach": [],
    "admin": ["org", "users", "groups", "tool-configs", "interfaces", "emails"],
}

# Alias so legacy internal gate still works (api-keys entered as 'apikeys' by some paths)
_COMMAND_PERMISSIONS_EXTRA: dict[str, str] = {"apikeys": "apikeys:personal"}

_TOP_LEVEL = sorted(_COMPLETIONS.keys())

# Editors tried in order when neither $VISUAL nor $EDITOR is set
_FALLBACK_EDITORS = ("nano", "vim", "vi", "notepad")

# Subcomandos que esperam um ID como terceiro token
_APPROVAL_ID_SUBCMDS = {"show", "approve", "reject"}
_CONVERSATION_ID_SUBCMDS = {"show", "archive"}
_DATASTORE_ID_SUBCMDS = {"show", "update", "delete", "records", "record", "add-record", "update-record", "delete-record", "query"}
_FILE_ID_SUBCMDS = {"download", "delete"}
_AGENT_ID_SUBCMDS = {"show", "update", "activate", "deactivate", "delete"}
_APPROVAL_RULE_ID_SUBCMDS = {"show", "update", "activate", "deactivate", "delete"}
_DEPT_SET_SUBCMDS = {"set"}

_ID_CACHE_TTL = 30  # segundos


def _complete_path(text: str) -> list[str]:
    """Return filesystem path completions for *text* (used for file arguments)."""
    if not text:
        return []
    expanded = os.path.expanduser(text)
    matches = glob.glob(expanded + "*")
    result = []
    for m in sorted(matches):
        result.append(m + "/" if os.path.isdir(m) else m)
    return result


class _Completer:
    """readline-compatible completer for gSage AI CLI commands.

    Works like bash completion:
    - First token:  completes command names.
    - Second token: completes subcommands.
    - Third token:  completes IDs fetched from the API (with 30s cache).
    """

    def __init__(self, client: GSageAPIClient, state: "REPLState | None" = None) -> None:
        self._client = client
        self._state = state
        self._matches: list[str] = []
        # cache: key → (timestamp, [ids])
        self._cache: dict[str, tuple[float, list[str]]] = {}

    def invalidate_approval_cache(self) -> None:
        """Call after approve/reject so next Tab fetches fresh IDs."""
        self._cache.pop("approvals", None)

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            self._matches = self._get_matches(text)
        try:
            return self._matches[state]
        except IndexError:
            return None

    def _get_matches(self, text: str) -> list[str]:
        try:
            line = readline.get_line_buffer()
        except Exception:
            return []

        parts = line.lstrip().split()
        word_index = len(parts) - (0 if line.endswith(" ") else 1)

        if word_index == 0:
            return [c + " " for c in _TOP_LEVEL if c.startswith(text)]

        command = parts[0].lower()

        if word_index == 1:
            # Filesystem path completion for '/attach <path>'
            if command == "attach":
                return _complete_path(text)
            subcommands = _COMPLETIONS.get(command, [])
            return [s + " " for s in subcommands if s.startswith(text)]

        if word_index == 2:
            subcmd = parts[1].lower() if len(parts) > 1 else ""
            # Filesystem path completion for 'knowledge ingest <path>'
            if command == "knowledge" and subcmd == "ingest":
                return _complete_path(text)
            ids = self._fetch_ids(command, subcmd)
            return [i for i in ids if i.startswith(text)]

        return []

    def _fetch_ids(self, command: str, subcmd: str) -> list[str]:
        """Return a list of IDs appropriate for the given command+subcommand.

        Results are cached for _ID_CACHE_TTL seconds to avoid hitting the
        API on every keystroke.
        """
        cache_key: str | None = None

        if command == "approvals" and subcmd in _APPROVAL_ID_SUBCMDS:
            cache_key = "approvals"
        elif command == "conversation" and subcmd in _CONVERSATION_ID_SUBCMDS:
            cache_key = "conversations"
        elif command == "datastores" and subcmd in _DATASTORE_ID_SUBCMDS:
            cache_key = "datastores"
        elif command == "files" and subcmd in _FILE_ID_SUBCMDS:
            cache_key = "files"
        elif command == "agents" and subcmd in _AGENT_ID_SUBCMDS:
            cache_key = "agents"
        elif command == "approval-rules" and subcmd in _APPROVAL_RULE_ID_SUBCMDS:
            cache_key = "approval_rules"
        elif command == "dept" and subcmd in _DEPT_SET_SUBCMDS:
            cache_key = "depts"
        elif command == "knowledge" and subcmd == "status":
            # Return job IDs stored in session state (no API call needed)
            if self._state is not None:
                return list(self._state.ingest_jobs)
            return []

        if cache_key is None:
            return []

        now = time.monotonic()
        if cache_key in self._cache:
            ts, ids = self._cache[cache_key]
            if now - ts < _ID_CACHE_TTL:
                return ids

        try:
            if cache_key == "approvals":
                # For approve/reject prefer pending; for show fetch all
                status = "pending" if subcmd in ("approve", "reject") else None
                data = self._client.list_approvals(approval_status=status, limit=100)
                ids = [str(item["id"]) for item in data.get("items", []) if item.get("id")]
            elif cache_key == "datastores":
                data = self._client.list_datastores(page=1, limit=100)
                ids = [str(s["id"]) for s in data.get("items", []) if s.get("id")]
            elif cache_key == "files":
                data = self._client.list_files(page=1, limit=100)
                ids = [str(f["id"]) for f in data.get("items", []) if f.get("id")]
            elif cache_key == "agents":
                data = self._client.list_scheduled_jobs(page=1, limit=100)
                ids = [str(j["id"]) for j in data.get("items", []) if j.get("id")]
            elif cache_key == "approval_rules":
                data = self._client.list_approval_rules(page=1, limit=100)
                ids = [str(r["id"]) for r in data.get("items", []) if r.get("id")]
            elif cache_key == "depts":
                depts = self._client.list_departments()
                # Offer both slug and UUID so the user can pick whichever they prefer
                slugs = [d["slug"] for d in depts if d.get("slug")]
                uuids = [str(d["id"]) for d in depts if d.get("id")]
                ids = slugs + uuids
            else:
                data = self._client.list_conversations(page=1, limit=100)
                ids = [str(c["id"]) for c in data.get("items", []) if c.get("id")]
        except Exception:
            ids = []

        self._cache[cache_key] = (now, ids)
        return ids


@dataclass
class REPLState:
    """State for the REPL session."""

    conversation_id: str | None = None
    debug: bool = False
    output_format: str = "markdown"
    ingest_jobs: list[str] = field(default_factory=list)
    pending_attachment_ids: list[str] = field(default_factory=list)


class GSageREPL:
    """Interactive REPL for gSage AI."""

    def __init__(self, config: Config, client: GSageAPIClient):
        self.config = config
        self.client = client
        self.console = Console()
        self.state = REPLState(
            conversation_id=config.conversation_id,
            debug=config.debug,
            output_format=config.output_format,
        )
        self.history_file = Path.home() / ".gsage_ai_history"
        self._setup_history()

    def _setup_history(self) -> None:
        """Load command history from file and configure readline."""
        if not READLINE_AVAILABLE:
            if self.state.debug:
                logger.debug("readline not available, command history disabled")
            return

        try:
            # Load existing history if file exists
            if self.history_file.exists():
                readline.read_history_file(str(self.history_file))
                if self.state.debug:
                    logger.debug(f"Loaded {readline.get_current_history_length()} history entries")

            # Set maximum history entries to prevent file bloat
            readline.set_history_length(1000)

            # Tab completion
            self._completer = _Completer(self.client, state=self.state)
            readline.set_completer(self._completer.complete)
            readline.set_completer_delims(" \t")
            readline.parse_and_bind("tab: complete")

        except Exception as exc:
            if self.state.debug:
                logger.debug(f"Failed to load history: {exc}")

    def _save_history(self) -> None:
        """Save command history to file."""
        if not READLINE_AVAILABLE:
            return

        try:
            # Ensure parent directory exists
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Write history to file
            readline.write_history_file(str(self.history_file))
            
            if self.state.debug:
                logger.debug(f"Saved {readline.get_current_history_length()} history entries")
                
        except Exception as exc:
            if self.state.debug:
                logger.debug(f"Failed to save history: {exc}")

    def run(self) -> None:
        """Start the interactive REPL loop."""
        self._show_welcome()

        while True:
            try:
                user_input = self._get_input()
                if not user_input:
                    continue

                # Parse command
                if not self._handle_command(user_input):
                    break  # Exit requested

            except KeyboardInterrupt:
                self.console.print(f"\n[{COLOR_ERROR}]Interrupted. Type 'exit' to quit.[/{COLOR_ERROR}]")
            except EOFError:
                self.console.print("\n[dim]Goodbye![/dim]")
                break
            except Exception as exc:
                self.console.print(f"[{COLOR_ERROR}]Unexpected error: {exc}[/{COLOR_ERROR}]")
                if self.state.debug:
                    logger.exception("REPL error")

        self._save_history()
        self._show_goodbye()

    def _show_welcome(self) -> None:
        """Display welcome banner."""
        banner = """
╔═══════════════════════════════════════════════════════════╗
║  gSage AI — CLI Client                     ║
║  Type 'help' for commands or just chat naturally          ║
╚═══════════════════════════════════════════════════════════╝
"""
        self.console.print(f"[bold bright_cyan]{banner}[/bold bright_cyan]")
        
        if self.state.conversation_id:
            self.console.print(f"[dim]Resuming conversation: {self.state.conversation_id}[/dim]\n")

    def _show_goodbye(self) -> None:
        """Display goodbye message."""
        self.console.print("\n[bold bright_cyan]Thank you for using gSage AI. Stay secure! 🛡️[/bold bright_cyan]\n")

    def _find_editor(self) -> str:
        """Return the editor command to use.

        Priority: $VISUAL → $EDITOR → first of nano/vim/vi/notepad in PATH.
        """
        for env_var in ("VISUAL", "EDITOR"):
            ed = os.environ.get(env_var, "").strip()
            if ed:
                return ed
        for ed in _FALLBACK_EDITORS:
            if shutil.which(ed):
                return ed
        return "vi"  # last resort

    def _open_editor(self) -> str | None:
        """Open an external editor to compose a multi-line message.

        Returns the stripped message content, or None if cancelled / empty.
        Comment lines (starting with '#') are stripped automatically.
        """
        editor = self._find_editor()
        placeholder = (
            "# gSage AI — message composer\n"
            "# Lines starting with '#' are ignored.\n"
            "# Save and exit the editor to send the message.\n"
            "# Delete all content (or keep only comment lines) to cancel.\n\n"
        )

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix="gsage-",
            delete=False,
            encoding="utf-8",
        ) as tf:
            tf.write(placeholder)
            tmp_path = Path(tf.name)

        try:
            result = subprocess.run([editor, str(tmp_path)])
            if result.returncode != 0:
                self.console.print(
                    f"[{COLOR_ERROR}]Editor exited with code {result.returncode}.[/{COLOR_ERROR}]"
                )
                return None

            raw = tmp_path.read_text(encoding="utf-8")
            lines = [line for line in raw.splitlines() if not line.startswith("#")]
            return "\n".join(lines).strip() or None

        except FileNotFoundError:
            self.console.print(
                f"[{COLOR_ERROR}]Editor not found: '{editor}'. "
                f"Set $EDITOR or $VISUAL to configure.[/{COLOR_ERROR}]"
            )
            return None
        except Exception as exc:
            self.console.print(f"[{COLOR_ERROR}]Editor error: {exc}[/{COLOR_ERROR}]")
            return None
        finally:
            tmp_path.unlink(missing_ok=True)

    def _get_input(self) -> str:
        """Get user input with styled prompt.
        
        Uses native input() instead of rich.Console.input() for readline compatibility.
        Rich's ANSI escape codes interfere with readline's cursor positioning when
        navigating command history with arrow keys.
        """
        return input("> ").strip()

    def _handle_command(self, user_input: str) -> bool:
        """Handle user input (command or message).
        
        Returns:
            True to continue REPL, False to exit
        """
        # Parse first token to check for commands
        tokens = user_input.split(maxsplit=1)
        if not tokens:
            return True

        command = tokens[0].lower()
        args_str = tokens[1] if len(tokens) > 1 else ""

        # Exit commands
        if command in ("exit", "quit"):
            return False

        # Help command
        if command == "help":
            if self.client.org_id:
                try:
                    self.client.get_me()
                except Exception:
                    pass  # silent fail — show help with whatever permissions we have
            show_help(self.console, self.client.permissions or None)
            return True

        # Clear screen
        if command == "clear":
            os.system("clear" if os.name != "nt" else "cls")
            return True

        # Debug toggle
        if command == "debug":
            toggle_debug(self.state, self.console)
            return True

        # Permission gate — soft check before dispatching restricted commands
        required_perm = CMD_PERMISSIONS.get(command) or _COMMAND_PERMISSIONS_EXTRA.get(command)
        if required_perm and self.client.permissions and required_perm not in self.client.permissions:
            self.console.print(
                f"[dim]⚠ Sem permissão para '{command}' "
                f"(requer: {required_perm}). "
                "Acesse com uma conta com permissões adequadas.[/dim]"
            )
            return True

        # Auth commands
        if command == "login":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_login_command(args, self.state, self.client, self.console)
            return True

        if command == "register":
            handle_register_command(self.state, self.client, self.console)
            return True

        if command == "whoami":
            handle_whoami_command(self.client, self.console, self.state)
            return True

        if command == "profile":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_profile_command(args, self.state, self.client, self.console)
            return True

        # Conversation management
        if command == "conversation":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()

            handle_conversation_command(args, self.state, self.client, self.console)
            return True

        # Messages listing
        if command == "messages":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            
            handle_messages_command(args, self.state, self.client, self.console)
            return True

        # Approvals (HITL)
        if command == "approvals":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_approvals_command(args, self.state, self.client, self.console)
            if hasattr(self, "_completer"):
                self._completer.invalidate_approval_cache()
            return True

        # Knowledge base
        if command == "knowledge":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_knowledge_command(args, self.state, self.client, self.console)
            return True

        # Files
        if command == "files":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_files_command(args, self.state, self.client, self.console)
            return True

        # Background tasks
        if command == "tasks":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_tasks_command(args, self.state, self.client, self.console)
            return True

        # API keys
        if command in ("api-keys", "apikeys"):
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_api_keys_command(args, self.state, self.client, self.console)
            return True

        # AI Agents
        if command == "agents":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_scheduled_jobs_command(args, self.state, self.client, self.console)
            return True

        # Approval rules
        if command == "approval-rules":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_approval_rules_command(args, self.state, self.client, self.console)
            return True

        # DataStores
        if command == "datastores":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_datastores_command(args, self.state, self.client, self.console)
            return True

        # Departments
        if command == "dept":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_dept_command(args, self.state, self.client, self.console)
            return True

        # OTP / 2FA management
        if command == "otp":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_otp_command(args, self.state, self.client, self.console)
            return True

        # Admin
        if command == "admin":
            try:
                args = shlex.split(args_str)
            except ValueError:
                args = args_str.split()
            handle_admin_command(args, self.state, self.client, self.console)
            return True

        # Attach a local file for the next message
        if command == "attach":
            if not args_str.strip():
                self.console.print("[dim]Usage: attach <file_path>[/dim]")
                return True
            file_path = Path(args_str.strip()).expanduser().resolve()
            if not file_path.is_file():
                self.console.print(f"[{COLOR_ERROR}]File not found: {file_path}[/{COLOR_ERROR}]")
                return True
            if not self.state.conversation_id:
                self.console.print(f"[{COLOR_ERROR}]No active conversation. Start one first.[/{COLOR_ERROR}]")
                return True
            try:
                import mimetypes
                content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
                data = file_path.read_bytes()
                result = self.client.upload_attachment(
                    conversation_id=self.state.conversation_id,
                    filename=file_path.name,
                    data=data,
                    content_type=content_type,
                )
                self.state.pending_attachment_ids.append(result["id"])
                self.console.print(
                    f"[dim]Attached: {file_path.name} (id: {result['id']}) — "
                    f"will be sent with your next message.[/dim]"
                )
            except Exception as exc:
                self.console.print(f"[{COLOR_ERROR}]Failed to upload attachment: {exc}[/{COLOR_ERROR}]")
            return True

        # Open external editor to compose a message
        if command == "editor":
            content = self._open_editor()
            if content is None:
                self.console.print("[dim]Editor cancelled or empty — nothing sent.[/dim]")
                return True
            self.console.print(f"\n[dim]Message preview:[/dim]")
            self.console.print(f"[{COLOR_USER}]{content}[/{COLOR_USER}]")
            self.console.print()
            try:
                confirm = input("Send? [Y/n] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[dim]Cancelled.[/dim]")
                return True
            if confirm in ("", "y", "yes"):
                _att_ids = list(self.state.pending_attachment_ids)
                self.state.pending_attachment_ids.clear()
                handle_send_message(content, self.state, self.client, self.console, attachment_ids=_att_ids or None)
            else:
                self.console.print("[dim]Message discarded.[/dim]")
            return True

        # Default: treat as message to send
        _att_ids = list(self.state.pending_attachment_ids)
        self.state.pending_attachment_ids.clear()
        handle_send_message(user_input, self.state, self.client, self.console, attachment_ids=_att_ids or None)
        return True
