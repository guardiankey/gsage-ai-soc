"""gSage AI — Active Directory LDAP client for ad_read.

Thin async wrapper over ``ldap3`` (synchronous under the hood, offloaded to
a thread so the asyncio loop is never blocked — same pattern as
:class:`src.shared.auth.backends.ldap_ad.LDAPAuthProvider`).

This module is used exclusively by :class:`AdReadTool`.  Writes go through
``_pwsh_runner`` and never touch this file.

Usage::

    async with AdClient(config) as client:
        users = await client.list_users(ou=None, name_pattern="alice*", limit=50)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

log = logging.getLogger(__name__)

# ── FILETIME conversion ───────────────────────────────────────────────
# Windows FILETIME is a 64-bit integer counting 100-nanosecond intervals
# since 1601-01-01T00:00:00Z.  Used to build LDAP comparison filters
# against pwdLastSet and lastLogonTimestamp.

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_NS_PER_SECOND = 10_000_000  # 100-ns intervals per second


def _datetime_to_filetime(dt: datetime) -> int:
    """Convert a timezone-aware datetime to a Windows FILETIME integer.

    The result is the number of 100-nanosecond intervals since
    1601-01-01T00:00:00Z — suitable for LDAP comparison filters
    against ``pwdLastSet`` and ``lastLogonTimestamp``.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - _FILETIME_EPOCH
    return int(delta.total_seconds() * _NS_PER_SECOND)


# ── Attribute sets ────────────────────────────────────────────────────
_USER_ATTRS: tuple[str, ...] = (
    "sAMAccountName",
    "distinguishedName",
    "userPrincipalName",
    "displayName",
    "givenName",
    "sn",
    "mail",
    "userAccountControl",
    "pwdLastSet",
    "lastLogonTimestamp",
    "whenCreated",
    "whenChanged",
    "memberOf",
    "description",
    "lockoutTime",
)

_GROUP_ATTRS: tuple[str, ...] = (
    "cn",
    "distinguishedName",
    "description",
    "groupType",
    "member",
    "whenCreated",
    "whenChanged",
)

_OU_ATTRS: tuple[str, ...] = (
    "ou",
    "distinguishedName",
    "description",
    "whenCreated",
)


class AdLdapError(Exception):
    """Raised when an LDAP operation fails in a way the tool should surface."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AdConnectionConfig:
    """Decoded subset of the tool config used for LDAP connections."""

    url: str
    bind_dn: str
    bind_password: str
    base_dn: str
    verify_ssl: bool = True
    connect_timeout: int = 10

    @classmethod
    def from_tool_config(cls, config: dict) -> "AdConnectionConfig":
        missing = [
            k for k in ("ldap_url", "ldap_bind_dn", "ldap_bind_password", "base_dn")
            if not config.get(k)
        ]
        if missing:
            raise AdLdapError(
                "CONFIG_INCOMPLETE",
                f"LDAP config incomplete — missing: {', '.join(missing)}",
            )
        return cls(
            url=config["ldap_url"],
            bind_dn=config["ldap_bind_dn"],
            bind_password=config["ldap_bind_password"],
            base_dn=config["base_dn"],
            verify_ssl=bool(config.get("ldap_verify_ssl", True)),
            connect_timeout=int(config.get("ldap_connect_timeout_seconds", 10) or 10),
        )


def _escape_filter(value: str) -> str:
    """Escape a string for safe inclusion in an LDAP filter."""
    try:
        from ldap3.utils.conv import escape_filter_chars  # type: ignore[import-not-found]

        return str(escape_filter_chars(value))
    except Exception:
        # Conservative fallback
        return (
            value.replace("\\", "\\5c")
            .replace("*", "\\2a")
            .replace("(", "\\28")
            .replace(")", "\\29")
            .replace("\x00", "\\00")
        )


def _glob_to_ldap(pattern: str) -> str:
    """Convert a glob-like pattern (with '*') into an LDAP filter fragment.

    Only ``*`` is translated; every other char is escaped.  ``?`` is treated
    as a literal (AD doesn't support per-char LDAP wildcards anyway).
    """
    parts = pattern.split("*")
    escaped = [_escape_filter(p) for p in parts]
    return "*".join(escaped)


# ---------------------------------------------------------------------------
# UserAccountControl flag decoding
# ---------------------------------------------------------------------------

# Minimal subset we surface to the LLM.  Full list:
# https://learn.microsoft.com/en-us/troubleshoot/windows-server/active-directory/useraccountcontrol-manipulate-account-properties
_UAC_DISABLED = 0x0002
_UAC_LOCKOUT = 0x0010  # not actually used for "locked out" — kept for reference
_UAC_PWD_NOTREQD = 0x0020
_UAC_PWD_CANT_CHANGE = 0x0040
_UAC_DONT_EXPIRE_PASSWORD = 0x10000


def _decode_uac(uac: Optional[int]) -> dict:
    if uac is None:
        return {}
    return {
        "raw": int(uac),
        "disabled": bool(uac & _UAC_DISABLED),
        "password_not_required": bool(uac & _UAC_PWD_NOTREQD),
        "password_cant_change": bool(uac & _UAC_PWD_CANT_CHANGE),
        "password_never_expires": bool(uac & _UAC_DONT_EXPIRE_PASSWORD),
    }


def _to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return None
    return str(value)


def _entry_to_user_dict(entry: Any) -> dict:
    """Serialize a user LDAP entry to a JSON-safe dict."""
    # ldap3 entries expose attributes as ``entry.<attr>.value``.  Some are
    # lists (memberOf), others scalars.  The ``entry_attributes_as_dict``
    # helper flattens everything.
    raw = entry.entry_attributes_as_dict if hasattr(entry, "entry_attributes_as_dict") else {}

    def _scalar(name: str) -> Any:
        val = raw.get(name)
        if isinstance(val, list):
            return val[0] if val else None
        return val

    uac = _scalar("userAccountControl")
    try:
        uac_int = int(uac) if uac is not None else None
    except (TypeError, ValueError):
        uac_int = None

    lockout_time_raw = _scalar("lockoutTime")
    try:
        lockout_int = int(lockout_time_raw) if lockout_time_raw is not None else 0
    except (TypeError, ValueError):
        lockout_int = 0

    member_of = raw.get("memberOf") or []
    if not isinstance(member_of, list):
        member_of = [member_of]

    return {
        "distinguished_name": _to_str(entry.entry_dn),
        "sam_account_name": _to_str(_scalar("sAMAccountName")),
        "user_principal_name": _to_str(_scalar("userPrincipalName")),
        "display_name": _to_str(_scalar("displayName")),
        "given_name": _to_str(_scalar("givenName")),
        "surname": _to_str(_scalar("sn")),
        "mail": _to_str(_scalar("mail")),
        "description": _to_str(_scalar("description")),
        "account_control": _decode_uac(uac_int),
        "locked_out": lockout_int > 0,
        "password_last_set": _to_str(_scalar("pwdLastSet")),
        "last_logon_timestamp": _to_str(_scalar("lastLogonTimestamp")),
        "when_created": _to_str(_scalar("whenCreated")),
        "when_changed": _to_str(_scalar("whenChanged")),
        "member_of": [_to_str(dn) for dn in member_of if dn],
    }


def _entry_to_group_dict(entry: Any) -> dict:
    raw = entry.entry_attributes_as_dict if hasattr(entry, "entry_attributes_as_dict") else {}

    def _scalar(name: str) -> Any:
        val = raw.get(name)
        if isinstance(val, list):
            return val[0] if val else None
        return val

    members = raw.get("member") or []
    if not isinstance(members, list):
        members = [members]

    return {
        "distinguished_name": _to_str(entry.entry_dn),
        "cn": _to_str(_scalar("cn")),
        "description": _to_str(_scalar("description")),
        "group_type_raw": _to_str(_scalar("groupType")),
        "member_count": len(members),
        "members": [_to_str(dn) for dn in members if dn],
        "when_created": _to_str(_scalar("whenCreated")),
        "when_changed": _to_str(_scalar("whenChanged")),
    }


def _entry_to_ou_dict(entry: Any) -> dict:
    raw = entry.entry_attributes_as_dict if hasattr(entry, "entry_attributes_as_dict") else {}

    def _scalar(name: str) -> Any:
        val = raw.get(name)
        if isinstance(val, list):
            return val[0] if val else None
        return val

    return {
        "distinguished_name": _to_str(entry.entry_dn),
        "ou": _to_str(_scalar("ou")),
        "description": _to_str(_scalar("description")),
        "when_created": _to_str(_scalar("whenCreated")),
    }


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------

@dataclass
class AdClient:
    """Async context-managed LDAP reader for Active Directory.

    The underlying ldap3 connection is synchronous; each operation is
    wrapped in ``asyncio.to_thread`` so the event loop stays responsive.
    """

    config: AdConnectionConfig
    _conn: Any = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> "AdClient":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    # ── Connection management ───────────────────────────────────────────

    async def _connect(self) -> None:
        try:
            await asyncio.to_thread(self._connect_sync)
        except AdLdapError:
            raise
        except Exception as exc:  # pragma: no cover — network/path errors
            raise AdLdapError("LDAP_CONNECT_FAILED", f"LDAP connection failed: {exc}") from exc

    def _connect_sync(self) -> None:
        try:
            import ldap3  # type: ignore[import-not-found]
            from ldap3 import Connection, Server, Tls  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise AdLdapError(
                "DEPENDENCY_MISSING",
                "ldap3 is required for ad_read. Add 'ldap3' to requirements.txt.",
            ) from exc

        import ssl as _ssl

        use_tls = self.config.url.lower().startswith("ldaps://")
        tls_config = None
        if use_tls:
            validate = _ssl.CERT_REQUIRED if self.config.verify_ssl else _ssl.CERT_NONE
            tls_config = Tls(validate=validate)

        server = Server(
            self.config.url,
            get_info=ldap3.NONE,
            tls=tls_config,
            connect_timeout=self.config.connect_timeout,
        )
        try:
            conn = Connection(
                server,
                user=self.config.bind_dn,
                password=self.config.bind_password,
                authentication=ldap3.SIMPLE,
                auto_bind=(
                    ldap3.AUTO_BIND_TLS_BEFORE_BIND
                    if use_tls
                    else ldap3.AUTO_BIND_NO_TLS
                ),
                receive_timeout=self.config.connect_timeout,
                raise_exceptions=False,
            )
        except Exception as exc:
            raise AdLdapError("LDAP_BIND_FAILED", f"LDAP bind failed: {exc}") from exc

        if not conn.bound:
            raise AdLdapError(
                "LDAP_BIND_FAILED",
                f"LDAP bind was rejected: {conn.last_error or 'unknown reason'}",
            )
        self._conn = conn

    async def _close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            await asyncio.to_thread(conn.unbind)
        except Exception:  # pragma: no cover
            log.debug("AdClient: unbind raised — ignoring", exc_info=True)

    # ── Queries ────────────────────────────────────────────────────────

    async def list_users(
        self,
        *,
        ou: Optional[str] = None,
        name_pattern: Optional[str] = None,
        enabled: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
        # ── Date filters (LDAP server-side) ─────────────────────────
        password_changed_within_days: Optional[int] = None,
        password_changed_older_than_days: Optional[int] = None,
        last_logon_within_days: Optional[int] = None,
        last_logon_older_than_days: Optional[int] = None,
    ) -> dict:
        """List users under *ou* (defaults to config.base_dn).

        Optional date filters are evaluated **server-side** by the DC via
        FILETIME integer comparisons — only matching entries are returned.
        """
        base = ou or self.config.base_dn
        filters: list[str] = ["(objectCategory=person)", "(objectClass=user)"]

        if name_pattern:
            pat = _glob_to_ldap(name_pattern)
            filters.append(f"(|(sAMAccountName={pat})(displayName={pat})(cn={pat}))")
        if enabled is True:
            filters.append("(!(userAccountControl:1.2.840.113556.1.4.803:=2))")
        elif enabled is False:
            filters.append("(userAccountControl:1.2.840.113556.1.4.803:=2)")

        # ── Mutual exclusion guards ──────────────────────────────────
        if password_changed_within_days is not None and password_changed_older_than_days is not None:
            raise AdLdapError(
                "INVALID_PARAMS",
                "Cannot combine password_changed_within_days and "
                "password_changed_older_than_days for the same field.",
            )
        if last_logon_within_days is not None and last_logon_older_than_days is not None:
            raise AdLdapError(
                "INVALID_PARAMS",
                "Cannot combine last_logon_within_days and "
                "last_logon_older_than_days for the same field.",
            )

        # ── lastLogonTimestamp replication guard ─────────────────────
        for param_name, param_val in (
            ("last_logon_within_days", last_logon_within_days),
            ("last_logon_older_than_days", last_logon_older_than_days),
        ):
            if param_val is not None and param_val < 14:
                raise AdLdapError(
                    "INVALID_PARAMS",
                    f"{param_name} must be >= 14. lastLogonTimestamp is "
                    "replicated only every ~14 days; values below 14 "
                    "return unreliable results.",
                )

        # ── Date filters (LDAP server-side FILETIME comparisons) ─────
        now = datetime.now(timezone.utc)

        if password_changed_within_days is not None:
            cutoff = now - timedelta(days=password_changed_within_days)
            filters.append(f"(pwdLastSet>={_datetime_to_filetime(cutoff)})")

        if password_changed_older_than_days is not None:
            cutoff = now - timedelta(days=password_changed_older_than_days)
            filters.append(f"(pwdLastSet<={_datetime_to_filetime(cutoff)})")

        if last_logon_within_days is not None:
            cutoff = now - timedelta(days=last_logon_within_days)
            filters.append(f"(lastLogonTimestamp>={_datetime_to_filetime(cutoff)})")

        if last_logon_older_than_days is not None:
            cutoff = now - timedelta(days=last_logon_older_than_days)
            filters.append(f"(lastLogonTimestamp<={_datetime_to_filetime(cutoff)})")

        ldap_filter = f"(&{''.join(filters)})"

        items, total = await self._search(
            base=base,
            filter_=ldap_filter,
            attrs=_USER_ATTRS,
            limit=limit,
            offset=offset,
            transform=_entry_to_user_dict,
        )

        # ── Auto-sort: when a date filter is active, results are always
        # sorted ascending (oldest / stalest first) — no params needed.
        password_filter = (
            password_changed_within_days is not None
            or password_changed_older_than_days is not None
        )
        last_logon_filter = (
            last_logon_within_days is not None
            or last_logon_older_than_days is not None
        )

        if last_logon_filter and password_filter:
            items.sort(key=lambda u: (
                u.get("last_logon_timestamp") or "",
                u.get("password_last_set") or "",
            ))
        elif last_logon_filter:
            items.sort(key=lambda u: u.get("last_logon_timestamp") or "")
        elif password_filter:
            items.sort(key=lambda u: u.get("password_last_set") or "")

        # ── Filter metadata for the agent ────────────────────────────
        applied_filters: dict[str, int] = {}
        cutoff_dates: dict[str, str] = {}

        if password_changed_within_days is not None:
            applied_filters["password_changed_within_days"] = password_changed_within_days
            cutoff_dates["password_changed_since"] = (
                now - timedelta(days=password_changed_within_days)
            ).isoformat()
        if password_changed_older_than_days is not None:
            applied_filters["password_changed_older_than_days"] = password_changed_older_than_days
            cutoff_dates["password_changed_before"] = (
                now - timedelta(days=password_changed_older_than_days)
            ).isoformat()
        if last_logon_within_days is not None:
            applied_filters["last_logon_within_days"] = last_logon_within_days
            cutoff_dates["last_logon_since"] = (
                now - timedelta(days=last_logon_within_days)
            ).isoformat()
        if last_logon_older_than_days is not None:
            applied_filters["last_logon_older_than_days"] = last_logon_older_than_days
            cutoff_dates["last_logon_before"] = (
                now - timedelta(days=last_logon_older_than_days)
            ).isoformat()

        sort_info: Optional[dict] = None
        if last_logon_filter:
            sort_info = {"by": "last_logon_timestamp", "order": "asc"}
        elif password_filter:
            sort_info = {"by": "password_last_set", "order": "asc"}

        hint: Optional[str] = None
        if last_logon_filter:
            hint = (
                "lastLogonTimestamp is replicated every ~14 days. "
                "Accounts that have never logged in interactively appear "
                "with last_logon_timestamp near the epoch (or null)."
            )

        return {
            "items": items,
            "count": len(items),
            "total_returned_by_server": total,
            "limit": limit,
            "offset": offset,
            "ou": base,
            "filters": applied_filters or None,
            "cutoff_dates": cutoff_dates or None,
            "sort": sort_info,
            "_hint": hint,
        }

    async def list_groups(
        self,
        *,
        ou: Optional[str] = None,
        name_pattern: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        base = ou or self.config.base_dn
        filters: list[str] = ["(objectClass=group)"]
        if name_pattern:
            pat = _glob_to_ldap(name_pattern)
            filters.append(f"(|(cn={pat})(sAMAccountName={pat}))")
        ldap_filter = f"(&{''.join(filters)})"

        items, total = await self._search(
            base=base,
            filter_=ldap_filter,
            attrs=_GROUP_ATTRS,
            limit=limit,
            offset=offset,
            transform=_entry_to_group_dict,
        )
        return {
            "items": items,
            "count": len(items),
            "total_returned_by_server": total,
            "limit": limit,
            "offset": offset,
            "ou": base,
        }

    async def list_ous(
        self,
        *,
        base_dn: Optional[str] = None,
        mode: str = "flat",
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        base = base_dn or self.config.base_dn
        # objectClass=organizationalUnit — AD and most LDAP dirs agree.
        ldap_filter = "(objectClass=organizationalUnit)"
        # 'mode' could affect scope in the future; we search SUBTREE regardless
        # and the caller can choose to display hierarchy.
        items, total = await self._search(
            base=base,
            filter_=ldap_filter,
            attrs=_OU_ATTRS,
            limit=limit,
            offset=offset,
            transform=_entry_to_ou_dict,
        )
        return {
            "items": items,
            "count": len(items),
            "total_returned_by_server": total,
            "limit": limit,
            "offset": offset,
            "base_dn": base,
            "mode": mode,
        }

    async def get_user(
        self,
        *,
        user_dn: Optional[str] = None,
        sam_account_name: Optional[str] = None,
    ) -> Optional[dict]:
        if user_dn:
            base = user_dn
            ldap_filter = "(objectClass=user)"
            scope_base = True
        elif sam_account_name:
            base = self.config.base_dn
            ldap_filter = f"(&(objectCategory=person)(sAMAccountName={_escape_filter(sam_account_name)}))"
            scope_base = False
        else:
            raise AdLdapError(
                "INVALID_PARAMS",
                "get_user requires either 'user_dn' or 'sam_account_name'.",
            )
        items, _ = await self._search(
            base=base,
            filter_=ldap_filter,
            attrs=_USER_ATTRS,
            limit=1,
            offset=0,
            transform=_entry_to_user_dict,
            scope_base=scope_base,
        )
        return items[0] if items else None

    async def get_group(
        self,
        *,
        group_dn: Optional[str] = None,
        group_name: Optional[str] = None,
    ) -> Optional[dict]:
        if group_dn:
            base = group_dn
            ldap_filter = "(objectClass=group)"
            scope_base = True
        elif group_name:
            base = self.config.base_dn
            ldap_filter = (
                f"(&(objectClass=group)(cn={_escape_filter(group_name)}))"
            )
            scope_base = False
        else:
            raise AdLdapError(
                "INVALID_PARAMS",
                "get_group requires either 'group_dn' or 'group_name'.",
            )
        items, _ = await self._search(
            base=base,
            filter_=ldap_filter,
            attrs=_GROUP_ATTRS,
            limit=1,
            offset=0,
            transform=_entry_to_group_dict,
            scope_base=scope_base,
        )
        return items[0] if items else None

    # ── Security audit ────────────────────────────────────────────────

    async def audit_accounts(
        self,
        *,
        ou: Optional[str] = None,
        categories: list[str],
        stale_days: int = 90,
        password_change_days: int = 30,
        include_items: bool = False,
    ) -> dict:
        """Run multiple security-focused LDAP queries and return a summary.

        Each category runs as a separate LDAP search within the same
        connection.  Counts are fetched via :meth:`_count` (server-side
        tally, zero data transfer) unless *include_items* is ``True``.
        """
        base = ou or self.config.base_dn
        now = datetime.now(timezone.utc)

        category_specs: dict[str, dict] = {
            "stale_accounts": {
                "filter": (
                    f"(&(objectCategory=person)(objectClass=user)"
                    f"(!(userAccountControl:1.2.840.113556.1.4.803:=2))"
                    f"(lastLogonTimestamp<={_datetime_to_filetime(now - timedelta(days=stale_days))}))"
                ),
                "threshold_days": stale_days,
                "severity": "high",
            },
            "recent_password_changes": {
                "filter": (
                    f"(&(objectCategory=person)(objectClass=user)"
                    f"(pwdLastSet>={_datetime_to_filetime(now - timedelta(days=password_change_days))}))"
                ),
                "threshold_days": password_change_days,
                "severity": "info",
            },
            "locked_out": {
                "filter": "(&(objectCategory=person)(objectClass=user)(lockoutTime>=1))",
                "threshold_days": None,
                "severity": "medium",
            },
            "password_never_expires": {
                "filter": (
                    "(&(objectCategory=person)(objectClass=user)"
                    "(userAccountControl:1.2.840.113556.1.4.803:=65536))"
                ),
                "threshold_days": None,
                "severity": "medium",
            },
            "never_logged_in": {
                "filter": (
                    "(&(objectCategory=person)(objectClass=user)"
                    "(|(lastLogonTimestamp=0)(!(lastLogonTimestamp=*))))"
                ),
                "threshold_days": None,
                "severity": "high",
            },
        }

        # Resolve "all" → every defined category
        resolved: list[str] = []
        for c in categories:
            if c == "all":
                resolved.extend(k for k in category_specs if k not in resolved)
            elif c not in resolved:
                resolved.append(c)

        findings: dict = {}
        for cat_key in resolved:
            spec = category_specs.get(cat_key)
            if spec is None:
                continue

            if include_items:
                items, total = await self._search(
                    base=base,
                    filter_=spec["filter"],
                    attrs=_USER_ATTRS,
                    limit=500,
                    offset=0,
                    transform=_entry_to_user_dict,
                )
                # Sort stalest / oldest first (ascending) for consistency
                if cat_key in ("stale_accounts", "never_logged_in"):
                    items.sort(key=lambda u: u.get("last_logon_timestamp") or "")
                elif cat_key == "recent_password_changes":
                    items.sort(key=lambda u: u.get("password_last_set") or "")
                elif cat_key == "locked_out":
                    items.sort(key=lambda u: u.get("sam_account_name") or "")
                elif cat_key == "password_never_expires":
                    items.sort(key=lambda u: u.get("sam_account_name") or "")
            else:
                total = await self._count(base, spec["filter"])
                items = []

            finding: dict = {
                "count": total,
                "severity": spec["severity"],
            }
            if spec["threshold_days"] is not None:
                finding["threshold_days"] = spec["threshold_days"]
            if include_items:
                finding["items"] = items
            findings[cat_key] = finding

        # ── Overall summary counts ──────────────────────────────────
        total_all = await self._count(
            base, "(&(objectCategory=person)(objectClass=user))"
        )
        enabled_count = await self._count(
            base,
            "(&(objectCategory=person)(objectClass=user)"
            "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))",
        )
        disabled_count = await self._count(
            base,
            "(&(objectCategory=person)(objectClass=user)"
            "(userAccountControl:1.2.840.113556.1.4.803:=2))",
        )

        return {
            "ou": base,
            "summary": {
                "total_users": total_all,
                "enabled_users": enabled_count,
                "disabled_users": disabled_count,
            },
            "findings": findings,
        }

    # ── Internal helpers ────────────────────────────────────────────────

    async def _count(self, base: str, filter_: str) -> int:
        """Return the number of entries matching *filter_*.

        Runs a dedicated LDAP search with ``size_limit=0`` (server default,
        typically 1000 entries in AD) and DN-only attributes.  No entry
        data is transferred — only the count matters.

        For accurate counts in directories exceeding ``MaxPageSize``
        (default 1000), a paged-results control is needed.  This
        implementation is good enough for SOC domains of typical size.
        """
        return await asyncio.to_thread(self._count_sync, base, filter_)

    def _count_sync(self, base: str, filter_: str) -> int:
        import ldap3  # type: ignore[import-not-found]

        conn = self._conn
        if conn is None or not getattr(conn, "bound", False):
            raise AdLdapError("LDAP_NOT_CONNECTED", "LDAP connection is not open.")

        try:
            ok = conn.search(
                search_base=base,
                search_filter=filter_,
                search_scope=ldap3.SUBTREE,
                attributes=["distinguishedName"],
                size_limit=0,  # server default limit (typically 1000)
            )
        except Exception as exc:
            raise AdLdapError(
                "LDAP_SEARCH_FAILED",
                f"LDAP count search failed: {exc}",
            ) from exc

        if not ok:
            err = getattr(conn, "last_error", None) or getattr(conn, "result", {})
            raise AdLdapError("LDAP_SEARCH_FAILED", f"LDAP count search failed: {err}")

        entries = list(getattr(conn, "entries", []) or [])
        return len(entries)

    async def _search(
        self,
        *,
        base: str,
        filter_: str,
        attrs: tuple[str, ...],
        limit: int,
        offset: int,
        transform,
        scope_base: bool = False,
    ) -> tuple[list[dict], int]:
        return await asyncio.to_thread(
            self._search_sync,
            base=base,
            filter_=filter_,
            attrs=attrs,
            limit=limit,
            offset=offset,
            transform=transform,
            scope_base=scope_base,
        )

    def _search_sync(
        self,
        *,
        base: str,
        filter_: str,
        attrs: tuple[str, ...],
        limit: int,
        offset: int,
        transform,
        scope_base: bool,
    ) -> tuple[list[dict], int]:
        import ldap3  # type: ignore[import-not-found]

        conn = self._conn
        if conn is None or not getattr(conn, "bound", False):
            raise AdLdapError("LDAP_NOT_CONNECTED", "LDAP connection is not open.")

        scope = ldap3.BASE if scope_base else ldap3.SUBTREE
        # Ask for offset+limit, then slice — good enough for admin reads.
        # For very large directories, switch to a paged-results control.
        requested = max(1, offset + limit)
        try:
            ok = conn.search(
                search_base=base,
                search_filter=filter_,
                search_scope=scope,
                attributes=list(attrs),
                size_limit=requested,
            )
        except Exception as exc:
            raise AdLdapError("LDAP_SEARCH_FAILED", f"LDAP search failed: {exc}") from exc

        if not ok:
            err = getattr(conn, "last_error", None) or getattr(conn, "result", {})
            raise AdLdapError("LDAP_SEARCH_FAILED", f"LDAP search failed: {err}")

        entries = list(getattr(conn, "entries", []) or [])
        total = len(entries)
        sliced = entries[offset : offset + limit]
        return [transform(e) for e in sliced], total


@asynccontextmanager
async def open_ad_client(config: dict) -> AsyncIterator[AdClient]:
    """Convenience factory returning an open :class:`AdClient`.

    Usage::

        async with open_ad_client(config) as client:
            users = await client.list_users(...)
    """
    cfg = AdConnectionConfig.from_tool_config(config)
    async with AdClient(cfg) as client:
        yield client
