"""gSage AI — Active Directory write safety guards.

Centralizes the fail-closed checks every ``ad_write`` action runs *before*
touching the jump host:

1. :func:`assert_write_enabled`  — honour ``config.write_enabled``.
2. :func:`assert_not_protected`  — block principals that match
   ``config.protected_users`` / ``config.protected_groups``.
3. :func:`generate_password`     — produce a compliant one-time password
   for ``reset_password`` / ``create_user``.
4. :func:`extract_sam_from_dn`   — helper for the protected-list matcher.
"""

from __future__ import annotations

import logging
import secrets
import string
from fnmatch import fnmatchcase
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception shared with ad_write — converts to ToolResult.failure(...)
# ---------------------------------------------------------------------------

class AdWriteBlocked(Exception):
    """Raised when a safety guard blocks an ad_write action."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# write_enabled gate
# ---------------------------------------------------------------------------

def assert_write_enabled(config: dict) -> None:
    """Raise :class:`AdWriteBlocked` when config.write_enabled is not true.

    Fail-closed: missing / non-boolean / false → blocked.
    """
    if not bool(config.get("write_enabled")):
        raise AdWriteBlocked(
            "CONFIG_WRITE_DISABLED",
            (
                "AD write operations are disabled for this org. "
                "An administrator must set config.write_enabled=true "
                "on the ad_write tool configuration."
            ),
        )


# ---------------------------------------------------------------------------
# Protected-list matching
# ---------------------------------------------------------------------------

def extract_sam_from_dn(dn: str) -> Optional[str]:
    """Return the leftmost RDN value (e.g. 'Administrator' from 'CN=Administrator,...').

    Strips the attribute prefix (``CN=`` / ``OU=`` / ``UID=``) and returns
    the value.  Returns None when *dn* doesn't look like a DN.
    """
    if not dn:
        return None
    first_rdn = dn.split(",", 1)[0].strip()
    if "=" not in first_rdn:
        return None
    return first_rdn.split("=", 1)[1].strip()


def _match_protected(
    *,
    target_dn: Optional[str],
    target_sam: Optional[str],
    protected: Iterable[str],
) -> Optional[str]:
    """Return the matching pattern (from *protected*) or None."""
    dn_lower = target_dn.lower() if target_dn else None
    sam_lower = target_sam.lower() if target_sam else None

    for raw in protected:
        if not raw:
            continue
        entry = raw.strip()
        if not entry:
            continue
        entry_lower = entry.lower()

        # Exact DN match
        if dn_lower and entry_lower == dn_lower:
            return entry
        # Exact sAMAccountName / CN match
        if sam_lower and entry_lower == sam_lower:
            return entry
        # Glob match on DN
        if dn_lower and "*" in entry_lower and fnmatchcase(dn_lower, entry_lower):
            return entry
        # Glob match on sAMAccountName / CN
        if sam_lower and "*" in entry_lower and fnmatchcase(sam_lower, entry_lower):
            return entry
    return None


def assert_user_not_protected(
    *,
    user_dn: Optional[str],
    sam_account_name: Optional[str],
    config: dict,
) -> None:
    """Raise :class:`AdWriteBlocked` when the target user is on the protected list."""
    protected = config.get("protected_users") or []
    resolved_sam = sam_account_name or extract_sam_from_dn(user_dn or "")
    match = _match_protected(
        target_dn=user_dn,
        target_sam=resolved_sam,
        protected=protected,
    )
    if match:
        raise AdWriteBlocked(
            "TARGET_PROTECTED",
            (
                f"The target user is in the protected list "
                f"(matched entry: '{match}'). Edit config.protected_users "
                f"if this block is intentional to be lifted."
            ),
        )


def assert_group_not_protected(
    *,
    group_dn: Optional[str],
    group_name: Optional[str],
    config: dict,
) -> None:
    """Raise :class:`AdWriteBlocked` when the target group is on the protected list."""
    protected = config.get("protected_groups") or []
    resolved_cn = group_name or extract_sam_from_dn(group_dn or "")
    match = _match_protected(
        target_dn=group_dn,
        target_sam=resolved_cn,
        protected=protected,
    )
    if match:
        raise AdWriteBlocked(
            "TARGET_PROTECTED",
            (
                f"The target group is in the protected list "
                f"(matched entry: '{match}'). Edit config.protected_groups "
                f"if this block is intentional to be lifted."
            ),
        )


# ---------------------------------------------------------------------------
# Password generation
# ---------------------------------------------------------------------------

_PASSWORD_UPPER = string.ascii_uppercase
_PASSWORD_LOWER = string.ascii_lowercase
_PASSWORD_DIGITS = string.digits
# AD-friendly symbols — avoid chars that are awkward in pwsh strings.
_PASSWORD_SYMBOLS = "!@#$%^&*()-_=+[]{}"


def generate_password(length: int = 16) -> str:
    """Generate a cryptographically strong password that satisfies default
    AD complexity requirements (at least one from each of upper / lower /
    digit / symbol)."""
    if length < 8:
        length = 8
    if length > 128:
        length = 128

    pools = [
        _PASSWORD_UPPER,
        _PASSWORD_LOWER,
        _PASSWORD_DIGITS,
        _PASSWORD_SYMBOLS,
    ]
    required = [secrets.choice(p) for p in pools]
    remaining_len = length - len(required)
    alphabet = "".join(pools)
    filler = [secrets.choice(alphabet) for _ in range(remaining_len)]
    chars = required + filler
    # Fisher-Yates-style shuffle using secrets.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)
