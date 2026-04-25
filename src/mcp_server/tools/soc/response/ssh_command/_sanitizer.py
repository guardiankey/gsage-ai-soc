"""gSage AI — SSH command sanitization utilities.

Provides argument sanitization for preset commands and allow/deny
pattern matching for arbitrary command execution.
"""

from __future__ import annotations

import re
from typing import Optional

# ── Default argument sanitization pattern ────────────────────────────────────
# Allows alphanumeric, dash, underscore, dot, forward-slash, colon, and space.
# Blocks shell metacharacters: ; & | ` $ ( ) { } [ ] < > \\ " ' ! # * ? ~ ^ %
DEFAULT_ARG_SANITIZE_PATTERN = r"^[a-zA-Z0-9._\-/: ]+$"

# ── Hardcoded deny list (always enforced, regardless of config) ───────────────
# Each entry is a compiled regex applied case-insensitively to the full command.
# These patterns block destructive or privilege-escalation commands that should
# never be allowed even if the operator misconfigures the allow/deny lists.
_HARDCODED_DENY_PATTERNS: list[re.Pattern] = [
    # Destructive filesystem operations
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s*/", re.IGNORECASE),   # rm -rf /
    re.compile(r"\bdd\s+if=", re.IGNORECASE),                           # dd if=...
    re.compile(r"\bmkfs\b", re.IGNORECASE),                             # mkfs.*
    re.compile(r"\bfdisk\b", re.IGNORECASE),                            # fdisk
    re.compile(r"\bparted\b", re.IGNORECASE),                           # parted
    re.compile(r"\bshred\b", re.IGNORECASE),                            # shred
    re.compile(r">\s*/dev/[sh]d[a-z]", re.IGNORECASE),                 # > /dev/sda
    re.compile(r"\btruncate\s+.*\s+/dev/", re.IGNORECASE),             # truncate /dev/
    # System power / reboot
    re.compile(r"\b(shutdown|reboot|poweroff|halt|init\s+[0-6])\b", re.IGNORECASE),
    re.compile(r"\bsystemctl\s+(poweroff|reboot|halt)\b", re.IGNORECASE),
    # Fork bomb
    re.compile(r":\(\)\s*\{", re.IGNORECASE),                          # :() { :|:& };:
    # Privilege escalation via setuid
    re.compile(r"\bchmod\s+[0-9]*[46][0-9]*\s", re.IGNORECASE),       # chmod +s / 4755
    re.compile(r"\bchmod\s+\+s\b", re.IGNORECASE),
    # Wipe everything under root
    re.compile(r"\brm\s+.*\s+/\s*$", re.IGNORECASE),                   # rm ... /
    re.compile(r"\brm\s+-[a-z]*f[a-z]*\s+[/~]", re.IGNORECASE),       # rm -f /*
    # Crontab backdoor
    re.compile(r"\bcrontab\s+-[a-z]*r\b", re.IGNORECASE),              # crontab -r
    # Write to /etc/passwd or /etc/shadow
    re.compile(r"(>>|>|\btee\b)\s*/etc/(passwd|shadow|sudoers)", re.IGNORECASE),
    # Drop SSH authorized_keys
    re.compile(r"(>>|>|\btee\b)\s*.*authorized_keys", re.IGNORECASE),
    # Base64-piped execution (common in malware)
    re.compile(r"\bbase64\s+(-d|--decode)\b.*\|\s*(ba)?sh", re.IGNORECASE),
    re.compile(r"\|\s*(ba)?sh\s*<", re.IGNORECASE),
    # Python/Perl/Ruby one-liner exec (can be used for shell escapes)
    re.compile(r"\bpython[23]?\s+-c\b.*\bexec\b", re.IGNORECASE),
    re.compile(r"\bperl\s+-e\b.*\bexec\b", re.IGNORECASE),
]


def sanitize_argument(value: str, pattern: str = DEFAULT_ARG_SANITIZE_PATTERN) -> tuple[str, Optional[str]]:
    """Validate and return a preset argument value.

    Args:
        value: Raw argument value from the LLM.
        pattern: Regex pattern the value must fully match. Use the per-preset
            ``argument_sanitize_pattern`` field, or the default.

    Returns:
        ``(value, None)`` if valid, ``("", error_message)`` if invalid.
    """
    if not isinstance(value, str):
        return "", f"Argument must be a string, got {type(value).__name__}"

    # Maximum argument length to prevent buffer issues
    if len(value) > 1024:
        return "", "Argument value exceeds maximum length (1024 characters)"

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        # Invalid pattern in config — fail safe
        return "", f"Invalid argument_sanitize_pattern in preset config: {exc}"

    if not compiled.fullmatch(value):
        return "", (
            f"Argument value {value!r} contains disallowed characters. "
            f"Allowed pattern: {pattern}"
        )
    return value, None


def check_hardcoded_deny(command: str) -> Optional[str]:
    """Return an error message if *command* matches any hardcoded deny pattern.

    Always called before executing any SSH command, regardless of tool type.
    Returns ``None`` if the command is allowed.
    """
    for pattern in _HARDCODED_DENY_PATTERNS:
        if pattern.search(command):
            return (
                f"Command blocked by hardcoded security policy "
                f"(matched pattern: {pattern.pattern!r}). "
                "This command cannot be executed regardless of configuration."
            )
    return None


def check_config_deny(command: str, denied_patterns: list[str]) -> Optional[str]:
    """Return an error message if *command* matches any operator-configured deny pattern.

    Args:
        command: Full command string to check.
        denied_patterns: List of regex strings from tool config.

    Returns:
        Error message string if denied, ``None`` if allowed.
    """
    for raw_pattern in denied_patterns:
        try:
            compiled = re.compile(raw_pattern, re.IGNORECASE)
        except re.error:
            # Skip invalid patterns — don't fail the tool over bad config
            continue
        if compiled.search(command):
            return (
                f"Command blocked by operator deny policy "
                f"(matched pattern: {raw_pattern!r})."
            )
    return None


def check_config_allow(command: str, allowed_patterns: list[str]) -> Optional[str]:
    """Return an error message if *command* does NOT match any operator allow pattern.

    Used only by SSHCommandTool (arbitrary commands). If ``allowed_patterns``
    is empty the check is skipped (all commands allowed, modulo deny lists).

    Args:
        command: Full command string to check.
        allowed_patterns: List of regex strings from tool config.

    Returns:
        Error message string if no pattern matched, ``None`` if allowed.
    """
    if not allowed_patterns:
        return None  # No allow list configured — everything passes (deny list still applies)

    for raw_pattern in allowed_patterns:
        try:
            compiled = re.compile(raw_pattern, re.IGNORECASE)
        except re.error:
            continue
        if compiled.search(command):
            return None  # At least one pattern matched

    return (
        "Command is not permitted: it did not match any of the configured "
        "allowed_command_patterns. Update the tool configuration to permit this command."
    )


def interpolate_preset(template: str, arguments: dict[str, str]) -> str:
    """Interpolate a preset command template with sanitized argument values.

    Template placeholders are ``{argument_name}``.  Only keys present in
    *arguments* are substituted.  Unknown placeholders are left as-is so the
    user gets a clear error from the remote shell rather than a silent no-op.

    Example:
        template = "ps auxw | grep {filter}"
        arguments = {"filter": "nginx"}
        → "ps auxw | grep nginx"
    """
    result = template
    for key, value in arguments.items():
        result = result.replace(f"{{{key}}}", value)
    return result
