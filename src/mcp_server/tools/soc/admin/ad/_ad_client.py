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
from typing import Any, AsyncIterator, Optional

log = logging.getLogger(__name__)


# Attribute sets used for each object type — kept small so we don't pull
# binary blobs (thumbnailPhoto, etc.) by accident.
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
    ) -> dict:
        """List users under *ou* (defaults to config.base_dn)."""
        base = ou or self.config.base_dn
        filters: list[str] = ["(objectCategory=person)", "(objectClass=user)"]
        if name_pattern:
            pat = _glob_to_ldap(name_pattern)
            filters.append(f"(|(sAMAccountName={pat})(displayName={pat})(cn={pat}))")
        if enabled is True:
            filters.append("(!(userAccountControl:1.2.840.113556.1.4.803:=2))")
        elif enabled is False:
            filters.append("(userAccountControl:1.2.840.113556.1.4.803:=2)")
        ldap_filter = f"(&{''.join(filters)})"

        items, total = await self._search(
            base=base,
            filter_=ldap_filter,
            attrs=_USER_ATTRS,
            limit=limit,
            offset=offset,
            transform=_entry_to_user_dict,
        )
        return {
            "items": items,
            "count": len(items),
            "total_returned_by_server": total,
            "limit": limit,
            "offset": offset,
            "ou": base,
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

    # ── Internal search helper ──────────────────────────────────────────

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
