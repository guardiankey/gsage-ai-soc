"""gSage AI — LDAPAuthProvider (LDAP / Active Directory).

Supports three authentication methods:
- SIMPLE  — plain LDAP bind (RFC 4513)
- NTLM    — NTLM v2 via ldap3's built-in NTLM support (Windows AD)
- KERBEROS — SASL GSSAPI via the optional ``gssapi`` library

All I/O is synchronous (ldap3 is not async-native) and wrapped in
``asyncio.to_thread`` so the FastAPI event loop is never blocked.

Dependencies
------------
- ``ldap3`` (required, pure Python)
- ``gssapi`` (optional, only needed for KERBEROS method)

Group mapping
-------------
The provider returns the raw list of LDAP group DNs the user belongs to.
The ``user_sync`` module maps those DNs to local ``GSageGroup`` names
using the ``group_mapping`` key in the provider config.

Example group_mapping::

    {
      "CN=SOC-Analysts,OU=Groups,DC=corp,DC=example,DC=com": {
        "role": "member",
        "groups": ["soc-analysts"]
      },
      "CN=SOC-Admins,OU=Groups,DC=corp,DC=example,DC=com": {
        "role": "admin",
        "groups": ["soc-admins", "soc-analysts"]
      }
    }

must_change_password detection
-------------------------------
The provider reads the ``pwdLastSet`` AD attribute.  When its value is
``0`` the admin has flagged "User must change password at next logon".
The ``AuthResult.must_change_password`` flag is set accordingly.  The
login route encodes this as a ``pwd_change_required`` JWT claim and also
exposes it in the ``TokenResponse`` body so that clients can act on it.

required_groups (login gate)
----------------------------
When ``required_groups`` is set to a non-empty list of LDAP group DNs, the
provider only allows login if the authenticating user belongs to **at least
one** of those groups.  Users who authenticate successfully against the LDAP
server but are not a member of any required group are rejected with
``ACCOUNT_DISABLED`` — which stops the auth chain so the request cannot fall
through to another provider (e.g. ``local``).

Group membership is resolved **before** the gate is evaluated, so nested
group resolution (``resolve_nested_groups=True``) is respected.

DistinguishedName comparison is **case-insensitive** (RFC 4514).

Example required_groups::

    [
        "CN=gSageAI-Users,OU=Security,DC=corp,DC=example,DC=com",
        "CN=SOC-Team,OU=Security,DC=corp,DC=example,DC=com"
    ]

Leave the list empty (default) to allow all valid LDAP users.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Optional

from src.shared.auth.base import (
    AuthErrorType,
    AuthIdentity,
    AuthResult,
    BaseAuthProvider,
)

logger = logging.getLogger(__name__)


def _escape_filter_chars(value: str) -> str:
    """Escape LDAP filter chars without hard dependency at import-time."""
    try:
        from ldap3.utils.conv import escape_filter_chars  # type: ignore[import-not-found]

        return str(escape_filter_chars(value))
    except Exception:
        return value


class LDAPAuthProvider(BaseAuthProvider):
    """Authenticate users against an LDAP server or Active Directory."""

    name = "ldap"
    display_name = "LDAP / Active Directory"

    config_defaults: ClassVar[dict] = {
        "server_url": "",
        "bind_dn": "",
        "bind_password": "",
        "user_search_base": "",
        "user_search_filter": "(sAMAccountName={username})",
        "group_search_base": "",
        "group_search_filter": "(member={user_dn})",
        "auth_method": "SIMPLE",        # SIMPLE | NTLM | KERBEROS
        "use_tls": True,
        "tls_validate": True,
        "kerberos_realm": "",
        "ntlm_domain": "",              # e.g. "CORP" for CORP\\username
        "connect_timeout_seconds": 10,
        "group_mapping": {},
        "default_role": "viewer",
        "auto_create_groups": True,
        "resolve_nested_groups": False,
        "required_groups": [],           # non-empty = login gate (list of group DNs)
    }

    config_schema: ClassVar[dict] = {
        "properties": {
            "server_url": {
                "type": "string",
                "description": "LDAP(S) URL, e.g. ldaps://dc.corp.example.com:636",
            },
            "bind_dn": {
                "type": "string",
                "description": "Service account DN used for user/group searches",
            },
            "bind_password": {
                "type": "string",
                "sensitive": True,
                "description": "Service account password",
            },
            "user_search_base": {
                "type": "string",
                "description": "LDAP base DN for user searches",
            },
            "user_search_filter": {
                "type": "string",
                "description": "LDAP filter with {username} placeholder, e.g. (sAMAccountName={username})",
            },
            "group_search_base": {
                "type": "string",
                "description": "LDAP base DN for group searches (leave empty to skip group sync)",
            },
            "group_search_filter": {
                "type": "string",
                "description": "LDAP filter with {user_dn} placeholder, e.g. (member={user_dn})",
            },
            "auth_method": {
                "type": "string",
                "description": "Authentication method: SIMPLE | NTLM | KERBEROS",
            },
            "use_tls": {
                "type": "boolean",
                "description": "Use LDAPS or STARTTLS",
            },
            "tls_validate": {
                "type": "boolean",
                "description": "Validate TLS certificate (set false only in dev/lab)",
            },
            "kerberos_realm": {
                "type": "string",
                "description": "Kerberos realm, e.g. CORP.EXAMPLE.COM (KERBEROS method only)",
            },
            "ntlm_domain": {
                "type": "string",
                "description": "NTLM domain prefix, e.g. CORP (NTLM method only)",
            },
            "connect_timeout_seconds": {
                "type": "integer",
                "description": "LDAP connection timeout in seconds",
            },
            "group_mapping": {
                "type": "object",
                "description": "Map external LDAP group DNs to local roles and groups",
            },
            "default_role": {
                "type": "string",
                "description": "Role assigned to users matched by no group_mapping entry",
            },
            "auto_create_groups": {
                "type": "boolean",
                "description": "Automatically create GSageGroup records for mapped groups",
            },
            "resolve_nested_groups": {
                "type": "boolean",
                "description": "Resolve nested (transitive) group memberships via memberOf:1.2.840.113556.1.4.1941:",
            },
            "required_groups": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Login gate: list of LDAP group DNs the user must belong to at least one of. "
                    "Empty list (default) allows all valid LDAP users."
                ),
            },
        },
        "required": ["server_url", "bind_dn", "bind_password", "user_search_base"],
    }

    # ── Attributes to fetch from the user entry ───────────────────────────
    _USER_ATTRS = [
        "distinguishedName",
        "mail",
        "userPrincipalName",
        "sAMAccountName",
        "displayName",
        "cn",
        "givenName",
        "sn",
        "objectGUID",
        "telephoneNumber",
        "mobile",
        "pwdLastSet",
        "userAccountControl",
        "memberOf",
    ]

    async def authenticate(
        self,
        username: str,
        password: str,
        config: dict,
    ) -> AuthResult:
        """Authenticate via LDAP.  All blocking I/O runs in a thread pool."""
        return await asyncio.to_thread(self._authenticate_sync, username, password, config)

    def _authenticate_sync(
        self, username: str, password: str, config: dict
    ) -> AuthResult:
        """Synchronous LDAP authentication (runs in a thread pool)."""
        try:
            import ldap3  # type: ignore[import-not-found]
            from ldap3 import (  # type: ignore[import-not-found]
                ALL_ATTRIBUTES,
                NTLM,
                SASL,
                Server,
                Connection,
                Tls,
                SUBTREE,
            )
            from ldap3.core.exceptions import (  # type: ignore[import-not-found]
                LDAPBindError,
                LDAPException,
                LDAPSocketOpenError,
                LDAPSocketReceiveError,
            )
        except ImportError:
            logger.error(
                "LDAPAuthProvider: 'ldap3' is not installed. "
                "Add 'ldap3' to requirements.txt."
            )
            return AuthResult(
                success=False,
                error_type=AuthErrorType.CONFIGURATION_ERROR,
                error_message="ldap3 library is not installed",
            )

        server_url: str = config.get("server_url", "")
        bind_dn: str = config.get("bind_dn", "")
        bind_password: str = config.get("bind_password", "")
        user_search_base: str = config.get("user_search_base", "")
        user_search_filter: str = config.get(
            "user_search_filter", "(sAMAccountName={username})"
        )
        auth_method: str = (config.get("auth_method") or "SIMPLE").upper()
        use_tls: bool = bool(config.get("use_tls", True))
        tls_validate: bool = bool(config.get("tls_validate", True))
        timeout: int = int(config.get("connect_timeout_seconds", 10))
        ntlm_domain: str = config.get("ntlm_domain", "")
        kerberos_realm: str = config.get("kerberos_realm", "")
        group_search_base: str = config.get("group_search_base", "")
        group_search_filter: str = config.get(
            "group_search_filter", "(member={user_dn})"
        )
        resolve_nested: bool = bool(config.get("resolve_nested_groups", False))

        if not server_url or not bind_dn or not user_search_base:
            return AuthResult(
                success=False,
                error_type=AuthErrorType.CONFIGURATION_ERROR,
                error_message="LDAPAuthProvider: server_url, bind_dn and user_search_base are required",
            )

        import ssl as _ssl

        # ── Build server ──────────────────────────────────────────────────
        tls_config = None
        if use_tls:
            validate_mode = _ssl.CERT_REQUIRED if tls_validate else _ssl.CERT_NONE
            tls_config = Tls(validate=validate_mode)

        try:
            server = Server(
                server_url,
                get_info=ldap3.ALL,
                tls=tls_config,
                connect_timeout=timeout,
            )
        except Exception as exc:
            return AuthResult(
                success=False,
                error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
                error_message=f"LDAP server configuration error: {exc}",
            )

        # ── Service account bind (to search the directory) ────────────────
        try:
            svc_conn = Connection(
                server,
                user=bind_dn,
                password=bind_password,
                authentication=ldap3.SIMPLE,
                auto_bind=ldap3.AUTO_BIND_TLS_BEFORE_BIND if use_tls else ldap3.AUTO_BIND_NO_TLS,
                receive_timeout=timeout,
            )
            svc_conn.bind()
        except Exception as exc:
            logger.error(
                "LDAPAuthProvider: service account bind failed to '%s': %s",
                server_url, exc,
            )
            return AuthResult(
                success=False,
                error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
                error_message=f"LDAP service bind failed: {exc}",
            )

        # ── Find user entry ───────────────────────────────────────────────
        search_filter = user_search_filter.replace("{username}", _escape_filter_chars(username))
        svc_conn.search(
            search_base=user_search_base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=self._USER_ATTRS,
        )

        if not svc_conn.entries:
            svc_conn.unbind()
            return AuthResult(
                success=False,
                error_type=AuthErrorType.USER_NOT_FOUND,
                error_message="User not found in LDAP directory",
            )

        user_entry = svc_conn.entries[0]
        user_dn: str = user_entry.entry_dn

        # ── Authenticate the user ─────────────────────────────────────────
        try:
            if auth_method == "NTLM":
                if ntlm_domain:
                    ntlm_user = f"{ntlm_domain}\\{username}"
                else:
                    ntlm_user = username
                user_conn = Connection(
                    server,
                    user=ntlm_user,
                    password=password,
                    authentication=NTLM,
                    receive_timeout=timeout,
                )
            elif auth_method == "KERBEROS":
                try:
                    import gssapi  # type: ignore[import-not-found]  # noqa: F401
                except ImportError:
                    svc_conn.unbind()
                    return AuthResult(
                        success=False,
                        error_type=AuthErrorType.CONFIGURATION_ERROR,
                        error_message="gssapi library is required for KERBEROS auth method",
                    )
                user_conn = Connection(
                    server,
                    authentication=SASL,
                    sasl_mechanism="GSSAPI",
                    sasl_credentials=(kerberos_realm or None,),
                    receive_timeout=timeout,
                )
            else:
                # SIMPLE — direct bind with user DN
                user_conn = Connection(
                    server,
                    user=user_dn,
                    password=password,
                    authentication=ldap3.SIMPLE,
                    auto_bind=ldap3.AUTO_BIND_TLS_BEFORE_BIND if use_tls else ldap3.AUTO_BIND_NO_TLS,
                    receive_timeout=timeout,
                )

            if not user_conn.bind():
                svc_conn.unbind()
                # Inspect the result description for well-known AD error codes
                result_desc: str = (user_conn.result or {}).get("description", "")
                error_code = self._extract_ad_error_code(
                    (user_conn.result or {}).get("message", "")
                )
                error_type = self._map_ad_error(error_code)
                return AuthResult(
                    success=False,
                    error_type=error_type,
                    error_message=f"LDAP bind failed: {result_desc}",
                )

        except Exception as exc:
            svc_conn.unbind()
            return AuthResult(
                success=False,
                error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
                error_message=f"LDAP connection error during user bind: {exc}",
            )

        user_conn.unbind()

        # ── Read identity attributes ──────────────────────────────────────
        email = self._get_attr(user_entry, "mail") or \
                self._get_attr(user_entry, "userPrincipalName") or \
                username
        full_name = (
            self._get_attr(user_entry, "displayName")
            or self._get_attr(user_entry, "cn")
            or f"{self._get_attr(user_entry, 'givenName') or ''} "
               f"{self._get_attr(user_entry, 'sn') or ''}".strip()
            or username
        )
        external_id = self._get_guid(user_entry)
        phone = (
            self._get_attr(user_entry, "mobile")
            or self._get_attr(user_entry, "telephoneNumber")
        )

        # ── must_change_password detection ───────────────────────────────
        must_change_pw = False
        pwd_last_set = self._get_attr(user_entry, "pwdLastSet")
        if pwd_last_set == "0" or pwd_last_set == 0:
            must_change_pw = True

        # ── Resolve group memberships ─────────────────────────────────────
        groups: list[str] = []
        if group_search_base:
            groups = self._resolve_groups(
                svc_conn=svc_conn,
                user_dn=user_dn,
                user_entry=user_entry,
                group_search_base=group_search_base,
                group_search_filter=group_search_filter,
                resolve_nested=resolve_nested,
            )
        else:
            # Fall back to memberOf attribute if present
            member_of = user_entry["memberOf"].values if "memberOf" in user_entry else []
            groups = list(member_of)

        svc_conn.unbind()

        # ── Required-groups gate (login gate) ─────────────────────────────
        required_groups: list[str] = config.get("required_groups") or []
        if required_groups:
            required_lower = {dn.lower() for dn in required_groups}
            user_groups_lower = {dn.lower() for dn in groups}
            if not required_lower.intersection(user_groups_lower):
                logger.warning(
                    "LDAPAuthProvider: user '%s' authenticated but is not a member "
                    "of any required group — access denied.",
                    username,
                )
                return AuthResult(
                    success=False,
                    error_type=AuthErrorType.ACCOUNT_DISABLED,
                    error_message="User is not a member of any required LDAP group",
                )

        identity = AuthIdentity(
            email=email,
            full_name=full_name,
            external_id=external_id,
            phone=phone,
        )

        return AuthResult(
            success=True,
            identity=identity,
            groups=groups,
            must_change_password=must_change_pw,
            extra_claims={
                "ldap_dn": user_dn,
                "sam_account_name": self._get_attr(user_entry, "sAMAccountName"),
            },
        )

    def _resolve_groups(
        self,
        svc_conn: Any,
        user_dn: str,
        user_entry: Any,
        group_search_base: str,
        group_search_filter: str,
        resolve_nested: bool,
    ) -> list[str]:
        """Return the list of group DNs the user belongs to."""
        try:
            import ldap3  # type: ignore[import-not-found]
            from ldap3 import SUBTREE  # type: ignore[import-not-found]

            if resolve_nested:
                # Use AD extensible match rule (LDAP_MATCHING_RULE_IN_CHAIN)
                nested_filter = (
                    f"(member:1.2.840.113556.1.4.1941:={_escape_filter_chars(user_dn)})"
                )
                svc_conn.search(
                    search_base=group_search_base,
                    search_filter=nested_filter,
                    search_scope=SUBTREE,
                    attributes=["distinguishedName"],
                )
            else:
                search_filter = group_search_filter.replace(
                    "{user_dn}", _escape_filter_chars(user_dn)
                )
                svc_conn.search(
                    search_base=group_search_base,
                    search_filter=search_filter,
                    search_scope=SUBTREE,
                    attributes=["distinguishedName"],
                )

            return [entry.entry_dn for entry in svc_conn.entries]
        except Exception as exc:
            logger.warning("LDAPAuthProvider: group search failed: %s", exc)
            # Fall back to memberOf attribute
            if "memberOf" in user_entry:
                return list(user_entry["memberOf"].values)
            return []

    @staticmethod
    def _get_attr(entry: Any, attr: str) -> Optional[str]:
        """Safely read a single-value LDAP attribute as string."""
        try:
            values = entry[attr].values
            if values:
                return str(values[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _get_guid(entry: Any) -> Optional[str]:
        """Read objectGUID as a hex string (stable identifier)."""
        try:
            raw = entry["objectGUID"].raw_values
            if raw:
                import uuid as _uuid
                return str(_uuid.UUID(bytes_le=raw[0]))
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_ad_error_code(message: str) -> Optional[str]:
        """Extract the hex AD error code from an LDAP error message string."""
        # AD error messages look like: "80090308: LdapErr: DSID-0C09044E, comment: AcceptSecurityContext error, data 532, ..."
        import re
        match = re.search(r"data\s+([0-9a-fA-F]+)", message or "")
        return match.group(1).lower() if match else None

    @staticmethod
    def _map_ad_error(code: Optional[str]) -> AuthErrorType:
        """Map AD error codes to AuthErrorType."""
        _AD_ERRORS: dict[str, AuthErrorType] = {
            "525": AuthErrorType.USER_NOT_FOUND,       # user not found
            "52e": AuthErrorType.INVALID_CREDENTIALS,  # invalid credentials
            "530": AuthErrorType.ACCOUNT_DISABLED,     # not permitted to logon at this time
            "531": AuthErrorType.ACCOUNT_DISABLED,     # not permitted to logon at this workstation
            "532": AuthErrorType.PASSWORD_EXPIRED,     # password expired
            "533": AuthErrorType.ACCOUNT_DISABLED,     # account disabled
            "534": AuthErrorType.ACCOUNT_DISABLED,     # user has not been granted the requested logon type
            "701": AuthErrorType.ACCOUNT_DISABLED,     # account expired
            "773": AuthErrorType.PASSWORD_EXPIRED,     # user must reset password
            "775": AuthErrorType.ACCOUNT_LOCKED,       # user account locked
        }
        if code is None:
            return AuthErrorType.INVALID_CREDENTIALS
        return _AD_ERRORS.get(code, AuthErrorType.INVALID_CREDENTIALS)

    async def healthcheck(self, config: dict) -> bool:
        """Test LDAP connectivity with the service account."""
        return await asyncio.to_thread(self._healthcheck_sync, config)

    def _healthcheck_sync(self, config: dict) -> bool:
        try:
            import ldap3  # type: ignore[import-not-found]
            server = ldap3.Server(
                config.get("server_url", ""),
                connect_timeout=int(config.get("connect_timeout_seconds", 10)),
            )
            conn = ldap3.Connection(
                server,
                user=config.get("bind_dn", ""),
                password=config.get("bind_password", ""),
                authentication=ldap3.SIMPLE,
            )
            ok = conn.bind()
            conn.unbind()
            return ok
        except Exception as exc:
            logger.warning("LDAPAuthProvider.healthcheck failed: %s", exc)
            return False
