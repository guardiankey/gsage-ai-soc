"""gSage AI — Active Directory tools: shared schemas.

Centralizes the ``config_schema`` and ``config_defaults`` reused by both
:class:`AdReadTool` and :class:`AdWriteTool`, and the per-action parameter
schema fragments used by ``ad_write``'s action dispatcher.

All sensitive fields (``ldap_bind_password``, ``ssh_private_key``,
``ssh_known_hosts``) are stored encrypted via :class:`GSageToolConfig`
(AES-256-GCM), like every other tool config in this project.

Two tools, one config row per org
---------------------------------
Both tools declare the same ``config_schema``.  An org-admin configures
the suite **once** under the shared logical key ``ad`` (see the
``config_defaults`` profile handling of :class:`BaseTool`).  Both tools
receive the same decrypted dict via ``load_config()``.

Fail-closed write gate
----------------------
``write_enabled`` defaults to ``False``.  The ``ad_write`` tool MUST
raise a ``CONFIG_WRITE_DISABLED`` failure when this flag is not true.

Protected principals
--------------------
``protected_users`` / ``protected_groups`` are matched against the
*target* of every write action.  Matching is case-insensitive and
accepts three forms per entry:

* plain ``sAMAccountName`` (``"Administrator"``) → matched against the
  sAMAccountName extracted from the target DN, or the name portion of a
  group DN.
* full DN (``"CN=Domain Admins,CN=Users,DC=corp,DC=local"``) → matched
  verbatim against the target DN.
* glob with ``*`` (``"svc-*"`` or ``"OU=Service Accounts,*"``) → matched
  against both forms with :func:`fnmatch`.

Action parameter validation
---------------------------
``ad_write`` uses a single ``params_schema`` with ``oneOf`` branches keyed
on the ``action`` discriminator.  Each branch enumerates exactly the
parameters that particular action needs, so the LLM gets a crisp contract
per action.
"""

from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Shared config_schema (both ad_read and ad_write declare this)
# ---------------------------------------------------------------------------

AD_CONFIG_SCHEMA: Final[dict] = {
    "type": "object",
    "properties": {
        # ── Domain ─────────────────────────────────────────────────────
        "domain": {
            "type": "string",
            "description": "AD domain DNS name, e.g. 'corp.contoso.local'.",
        },
        "base_dn": {
            "type": "string",
            "description": (
                "Base DN used as default search root for reads and as the "
                "organizational scope for protected-list matching. "
                "Example: 'DC=corp,DC=contoso,DC=local'."
            ),
        },
        # ── LDAP (reads) ───────────────────────────────────────────────
        "ldap_url": {
            "type": "string",
            "description": (
                "LDAP(S) server URL used by ad_read. "
                "Example: 'ldaps://dc01.corp.contoso.local:636'."
            ),
        },
        "ldap_bind_dn": {
            "type": "string",
            "description": "DN of the read-only service account used by ad_read.",
        },
        "ldap_bind_password": {
            "type": "string",
            "sensitive": True,
            "description": "Password for ldap_bind_dn (stored encrypted).",
        },
        "ldap_verify_ssl": {
            "type": "boolean",
            "description": "Validate the LDAP server TLS certificate. Default true.",
        },
        "ldap_connect_timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 120,
            "description": "Connection + operation timeout (seconds).",
        },
        # ── SSH + PowerShell (writes) ──────────────────────────────────
        "ssh_host": {
            "type": "string",
            "description": (
                "Windows jump host (FQDN or IP) with OpenSSH Server, "
                "PowerShell 7, and the RSAT ActiveDirectory module installed."
            ),
        },
        "ssh_port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "SSH port on the jump host (default 22).",
        },
        "ssh_user": {
            "type": "string",
            "description": (
                "Local Windows / AD user that PowerShell should impersonate. "
                "Must have permission to run the AD cmdlets used by ad_write."
            ),
        },
        "ssh_private_key": {
            "type": "string",
            "sensitive": True,
            "description": (
                "PEM/OpenSSH private key content for ssh_user "
                "(stored encrypted). Passphrase-less keys only."
            ),
        },
        "ssh_known_hosts": {
            "type": "string",
            "sensitive": True,
            "description": (
                "Optional OpenSSH known_hosts content used to pin the jump "
                "host's public key. When empty, host-key checking is "
                "disabled (operator is responsible for the network path)."
            ),
        },
        "ssh_command_timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 300,
            "description": "Maximum time to wait for a pwsh script to complete.",
        },
        # ── Safety & behaviour ─────────────────────────────────────────
        "write_enabled": {
            "type": "boolean",
            "description": (
                "Master switch for ad_write. When false, every write "
                "action fails with CONFIG_WRITE_DISABLED. Default false."
            ),
        },
        "protected_users": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Users that ad_write MUST NEVER touch. Matches sAMAccountName, "
                "full DN, or fnmatch glob (case-insensitive)."
            ),
        },
        "protected_groups": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Groups that ad_write MUST NEVER modify. Matches CN, full DN, "
                "or fnmatch glob (case-insensitive). Also blocks group "
                "membership changes where either side is protected."
            ),
        },
        "quarantine_ou": {
            "type": "string",
            "description": (
                "OU DN where disabled users are moved when "
                "disable_user is called with move_to_quarantine=true. "
                "Example: 'OU=Disabled Users,DC=corp,DC=contoso,DC=local'."
            ),
        },
        "log_actions_in_description": {
            "type": "boolean",
            "description": (
                "When true, ad_write appends a single-line action log to the "
                "target object's 'description' attribute after success. "
                "Format: '[YYYY-MM-DD HH:MM UTC] <action> by gSage: <summary>'."
            ),
        },
        "password_policy_length": {
            "type": "integer",
            "minimum": 8,
            "maximum": 128,
            "description": "Length of passwords generated by reset_password.",
        },
    },
    "additionalProperties": False,
}


AD_CONFIG_DEFAULTS: Final[dict] = {
    "domain": "",
    "base_dn": "",
    "ldap_url": "",
    "ldap_bind_dn": "",
    "ldap_bind_password": "",
    "ldap_verify_ssl": True,
    "ldap_connect_timeout_seconds": 10,
    "ssh_host": "",
    "ssh_port": 22,
    "ssh_user": "",
    "ssh_private_key": "",
    "ssh_known_hosts": "",
    "ssh_command_timeout_seconds": 60,
    "write_enabled": False,
    "protected_users": [
        "Administrator",
        "krbtgt",
        "Guest",
    ],
    "protected_groups": [
        "Domain Admins",
        "Enterprise Admins",
        "Schema Admins",
        "Administrators",
    ],
    "quarantine_ou": "",
    "log_actions_in_description": False,
    "password_policy_length": 16,
}


# ---------------------------------------------------------------------------
# ad_read: action discriminator + per-action params
# ---------------------------------------------------------------------------

AD_READ_ACTIONS: Final[tuple[str, ...]] = (
    "list_users",
    "list_groups",
    "list_ous",
    "get_user",
    "get_group",
    "audit_accounts",
)


AD_READ_PARAMS_SCHEMA: Final[dict] = {
    "type": "object",
    "required": ["action"],
    "properties": {
        "action": {
            "type": "string",
            "enum": list(AD_READ_ACTIONS),
            "description": (
                "Which read operation to perform. Parameters are "
                "action-specific — see oneOf branches."
            ),
        },
        # list_users / list_groups / list_ous pagination
        "ou": {
            "type": "string",
            "description": (
                "Base OU DN to search under (list_users / list_groups / "
                "list_ous). Defaults to config.base_dn when omitted."
            ),
        },
        "name_pattern": {
            "type": "string",
            "description": (
                "Optional name glob for filtering. Accepts '*' wildcards. "
                "Matches sAMAccountName / cn (case-insensitive)."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "list_users only: filter by account enabled/disabled state."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["flat", "tree"],
            "description": "list_ous only: 'flat' (default) or 'tree'.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "description": "Max rows returned (list_* actions). Default 100.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Pagination offset (list_* actions). Default 0.",
        },
        # ── Date filters (list_users, LDAP server-side) ────────────
        "password_changed_within_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3650,
            "description": "list_users only: filter to users whose pwdLastSet is within the last N days (recent password changes).",
        },
        "password_changed_older_than_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3650,
            "description": "list_users only: filter to users whose pwdLastSet is older than N days (stale passwords).",
        },
        "last_logon_within_days": {
            "type": "integer",
            "minimum": 14,
            "maximum": 3650,
            "description": "list_users only: filter to users whose lastLogonTimestamp is within the last N days (recently active). Min 14 due to AD replication lag.",
        },
        "last_logon_older_than_days": {
            "type": "integer",
            "minimum": 14,
            "maximum": 3650,
            "description": "list_users only: filter to users whose lastLogonTimestamp is older than N days (stale/inactive accounts). Min 14 due to AD replication lag.",
        },
        # ── CSV export (list_users, audit_accounts) ────────────────
        "export_csv": {
            "type": "boolean",
            "default": False,
            "description": "Persist all rows as a CSV file artifact. Prefer CSV for tabular results. Auto-forced when result exceeds 100 rows.",
        },
        # ── audit_accounts parameters ──────────────────────────────
        "audit_categories": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "stale_accounts",
                    "recent_password_changes",
                    "locked_out",
                    "password_never_expires",
                    "never_logged_in",
                    "all",
                ],
            },
            "description": "audit_accounts only: which audit categories to include. Use ['all'] for every category.",
        },
        "stale_days": {
            "type": "integer",
            "minimum": 14,
            "maximum": 3650,
            "default": 90,
            "description": "audit_accounts only: days threshold for stale_accounts category. Default 90.",
        },
        "password_change_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3650,
            "default": 30,
            "description": "audit_accounts only: days threshold for recent_password_changes category. Default 30.",
        },
        "include_items": {
            "type": "boolean",
            "default": False,
            "description": "audit_accounts only: include full user lists per category. When false, only counts are returned.",
        },
        # get_user / get_group
        "user_dn": {
            "type": "string",
            "description": "Full DN of the user (get_user).",
        },
        "sam_account_name": {
            "type": "string",
            "description": "sAMAccountName of the user (get_user).",
        },
        "group_dn": {
            "type": "string",
            "description": "Full DN of the group (get_group).",
        },
        "group_name": {
            "type": "string",
            "description": "CN of the group (get_group).",
        },
    },
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# ad_write: action discriminator + per-action params
# ---------------------------------------------------------------------------

AD_WRITE_ACTIONS: Final[tuple[str, ...]] = (
    "disable_user",
    "enable_user",
    "unlock_user",
    "reset_password",
    "force_password_change",
    "create_user",
    "modify_group_membership",
)


AD_WRITE_PARAMS_SCHEMA: Final[dict] = {
    "type": "object",
    "required": ["action"],
    "properties": {
        "action": {
            "type": "string",
            "enum": list(AD_WRITE_ACTIONS),
            "description": (
                "Which write operation to perform. Parameters are "
                "action-specific — see below. All actions require human approval."
            ),
        },
        "user_dn": {
            "type": "string",
            "description": (
                "Target user DN. Required for: disable_user, enable_user, "
                "unlock_user, reset_password, force_password_change, "
                "modify_group_membership."
            ),
        },
        "group_dn": {
            "type": "string",
            "description": "Target group DN. Required for: modify_group_membership.",
        },
        "move_to_quarantine": {
            "type": "boolean",
            "description": (
                "disable_user only: move the user to config.quarantine_ou "
                "after disabling. Default true."
            ),
        },
        "length": {
            "type": "integer",
            "minimum": 8,
            "maximum": 128,
            "description": (
                "reset_password only: length of the generated one-time password. "
                "Defaults to config.password_policy_length."
            ),
        },
        # create_user params
        "sam_account_name": {
            "type": "string",
            "description": (
                "create_user only: sAMAccountName for the new user (must be unique)."
            ),
        },
        "display_name": {
            "type": "string",
            "description": "create_user only: display name / full name.",
        },
        "given_name": {
            "type": "string",
            "description": "create_user only: first name.",
        },
        "surname": {
            "type": "string",
            "description": "create_user only: last name.",
        },
        "ou_dn": {
            "type": "string",
            "description": "create_user only: OU DN where the user will be created.",
        },
        "user_principal_name": {
            "type": "string",
            "description": "create_user only: UPN (e.g. 'alice@corp.local').",
        },
        "initial_password": {
            "type": "string",
            "description": (
                "create_user only: optional initial password. When omitted, "
                "a random one is generated and returned in the result."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "create_user only: enable the account immediately. Default false."
            ),
        },
        "groups": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "create_user only: list of group DNs or CNs the new user "
                "should be added to (subject to protected_groups)."
            ),
        },
        # modify_group_membership params
        "operation": {
            "type": "string",
            "enum": ["add", "remove"],
            "description": (
                "modify_group_membership only: whether to add or remove "
                "the user from the group."
            ),
        },
    },
    "additionalProperties": False,
}
