"""gSage AI — PowerShell script templates for ad_write.

Each public function returns a pwsh script body that:

* Imports the ActiveDirectory module.
* Performs one write operation.
* Optionally appends a single line to the target's ``description``.
* Emits a single ``ConvertTo-Json`` document as the last stdout line.

The scripts are designed to be passed to ``_pwsh_runner.run_pwsh_script``
via ``-EncodedCommand`` (UTF-16LE base64), so we have full control over
quoting and do NOT need to worry about SSH shell escaping.

IMPORTANT — parameter injection safety
--------------------------------------
Every value coming from the LLM / tool params is passed to pwsh through
:func:`_ps_quote`, which wraps the value in single quotes and doubles any
embedded single quotes (the PowerShell way to escape a literal ``'`` in a
single-quoted string — ``''``).  This prevents pwsh command injection
regardless of what the LLM puts in a DN / sAMAccountName / password.

SecureString passwords are built via ``ConvertTo-SecureString`` with
``-AsPlainText -Force`` on the remote host, so the plaintext never touches
the pwsh command line — it lives in the script body, which is base64-encoded
on transport.
"""

from __future__ import annotations

from typing import Optional


def _ps_quote(value: str) -> str:
    """Escape *value* for use inside a pwsh single-quoted string.

    In PowerShell single-quoted strings, the only metacharacter is ``'``
    itself, which is escaped by doubling (``''``).  This is the safest
    quoting style because no variable expansion or backtick processing
    occurs inside ``'...'``.
    """
    if value is None:
        return "''"
    return "'" + str(value).replace("'", "''") + "'"


def _ps_bool(value: bool) -> str:
    return "$true" if value else "$false"


def _description_log_snippet(
    *,
    enabled: bool,
    target_dn: str,
    action: str,
    summary: str,
) -> str:
    """Return a pwsh snippet that appends a log line to target's description.

    No-op when *enabled* is False.  Only appends when the resulting
    description stays under ~1024 chars (AD default limit).
    """
    if not enabled:
        return "# description logging disabled"
    target = _ps_quote(target_dn)
    action_s = _ps_quote(action)
    summary_s = _ps_quote(summary)
    return f"""
try {{
    $obj = Get-ADObject -Identity {target} -Properties Description
    $existing = [string]$obj.Description
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')
    $line = "[${{stamp}} UTC] {{0}} by gSageAI: {{1}}" -f {action_s}, {summary_s}
    if ($existing) {{ $newDesc = "$existing`n$line" }} else {{ $newDesc = $line }}
    if ($newDesc.Length -le 1024) {{
        Set-ADObject -Identity {target} -Description $newDesc -ErrorAction SilentlyContinue
    }}
}} catch {{
    Write-Warning "description log skipped: $($_.Exception.Message)"
}}
"""


_PREAMBLE = """$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { Import-Module ActiveDirectory -ErrorAction Stop } catch {
    @{ status = 'error'; code = 'MODULE_MISSING'; message = "ActiveDirectory PowerShell module not available on jump host: $($_.Exception.Message)" } | ConvertTo-Json -Compress
    exit 2
}
"""


def _wrap(action: str, body: str) -> str:
    """Wrap a single-action body with preamble + top-level try/catch."""
    return (
        _PREAMBLE
        + f"\n$__action = {_ps_quote(action)}\n"
        + "try {\n"
        + body
        + "\n} catch {\n"
        + "    @{ status = 'error'; action = $__action; code = 'PWSH_EXCEPTION'; message = $_.Exception.Message } | ConvertTo-Json -Compress\n"
        + "    exit 3\n"
        + "}\n"
    )


# ---------------------------------------------------------------------------
# disable_user
# ---------------------------------------------------------------------------

def disable_user_script(
    *,
    user_dn: str,
    quarantine_ou: Optional[str],
    log_in_description: bool,
    action_summary: str,
) -> str:
    target = _ps_quote(user_dn)
    quarantine = _ps_quote(quarantine_ou or "")
    move_section = ""
    if quarantine_ou:
        move_section = f"""
$target = Get-ADUser -Identity {target} -ErrorAction Stop
if ($target.DistinguishedName -ne {target}) {{
    # AD normalises DN casing — use the server-returned value
}}
Move-ADObject -Identity $target.DistinguishedName -TargetPath {quarantine} -ErrorAction Stop
"""
    log_snippet = _description_log_snippet(
        enabled=log_in_description,
        target_dn=user_dn,
        action="disable_user",
        summary=action_summary,
    )
    body = f"""
Disable-ADAccount -Identity {target} -ErrorAction Stop
{move_section}
{log_snippet}
@{{
    status = 'ok'
    action = $__action
    user_dn = {target}
    moved_to = {quarantine}
}} | ConvertTo-Json -Compress
"""
    return _wrap("disable_user", body)


# ---------------------------------------------------------------------------
# enable_user
# ---------------------------------------------------------------------------

def enable_user_script(
    *,
    user_dn: str,
    log_in_description: bool,
    action_summary: str,
) -> str:
    target = _ps_quote(user_dn)
    log_snippet = _description_log_snippet(
        enabled=log_in_description,
        target_dn=user_dn,
        action="enable_user",
        summary=action_summary,
    )
    body = f"""
Enable-ADAccount -Identity {target} -ErrorAction Stop
{log_snippet}
@{{ status = 'ok'; action = $__action; user_dn = {target} }} | ConvertTo-Json -Compress
"""
    return _wrap("enable_user", body)


# ---------------------------------------------------------------------------
# unlock_user
# ---------------------------------------------------------------------------

def unlock_user_script(
    *,
    user_dn: str,
    log_in_description: bool,
    action_summary: str,
) -> str:
    target = _ps_quote(user_dn)
    log_snippet = _description_log_snippet(
        enabled=log_in_description,
        target_dn=user_dn,
        action="unlock_user",
        summary=action_summary,
    )
    body = f"""
Unlock-ADAccount -Identity {target} -ErrorAction Stop
{log_snippet}
@{{ status = 'ok'; action = $__action; user_dn = {target} }} | ConvertTo-Json -Compress
"""
    return _wrap("unlock_user", body)


# ---------------------------------------------------------------------------
# reset_password
# ---------------------------------------------------------------------------

def reset_password_script(
    *,
    user_dn: str,
    new_password: str,
    log_in_description: bool,
    action_summary: str,
) -> str:
    target = _ps_quote(user_dn)
    pwd = _ps_quote(new_password)
    log_snippet = _description_log_snippet(
        enabled=log_in_description,
        target_dn=user_dn,
        action="reset_password",
        summary=action_summary,
    )
    body = f"""
$secure = ConvertTo-SecureString -String {pwd} -AsPlainText -Force
Set-ADAccountPassword -Identity {target} -Reset -NewPassword $secure -ErrorAction Stop
{log_snippet}
@{{ status = 'ok'; action = $__action; user_dn = {target} }} | ConvertTo-Json -Compress
"""
    return _wrap("reset_password", body)


# ---------------------------------------------------------------------------
# force_password_change
# ---------------------------------------------------------------------------

def force_password_change_script(
    *,
    user_dn: str,
    log_in_description: bool,
    action_summary: str,
) -> str:
    target = _ps_quote(user_dn)
    log_snippet = _description_log_snippet(
        enabled=log_in_description,
        target_dn=user_dn,
        action="force_password_change",
        summary=action_summary,
    )
    body = f"""
Set-ADUser -Identity {target} -ChangePasswordAtLogon $true -ErrorAction Stop
{log_snippet}
@{{ status = 'ok'; action = $__action; user_dn = {target} }} | ConvertTo-Json -Compress
"""
    return _wrap("force_password_change", body)


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------

def create_user_script(
    *,
    sam_account_name: str,
    display_name: str,
    given_name: Optional[str],
    surname: Optional[str],
    ou_dn: str,
    user_principal_name: Optional[str],
    initial_password: str,
    enabled: bool,
    groups: list[str],
    log_in_description: bool,
    action_summary: str,
) -> str:
    sam = _ps_quote(sam_account_name)
    disp = _ps_quote(display_name)
    given = _ps_quote(given_name or "")
    sur = _ps_quote(surname or "")
    ou = _ps_quote(ou_dn)
    upn = _ps_quote(user_principal_name or "")
    pwd = _ps_quote(initial_password)
    enabled_ps = _ps_bool(enabled)

    group_lines = []
    for g in groups:
        group_lines.append(f"Add-ADGroupMember -Identity {_ps_quote(g)} -Members $newUser -ErrorAction Stop")
    groups_section = "\n".join(group_lines) if group_lines else "# no groups"

    log_snippet = _description_log_snippet(
        enabled=log_in_description,
        target_dn="$($newUser.DistinguishedName)",  # injected via -f; see below
        action="create_user",
        summary=action_summary,
    )
    # For create_user, we don't know the DN until after creation, so we
    # craft the description log inline (not via _description_log_snippet).
    inline_log = ""
    if log_in_description:
        action_s = _ps_quote("create_user")
        summary_s = _ps_quote(action_summary)
        inline_log = f"""
try {{
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')
    $line = "[${{stamp}} UTC] {{0}} by gSageAI: {{1}}" -f {action_s}, {summary_s}
    Set-ADObject -Identity $newUser.DistinguishedName -Description $line -ErrorAction SilentlyContinue
}} catch {{
    Write-Warning "description log skipped: $($_.Exception.Message)"
}}
"""
    _ = log_snippet  # not used for create; kept to honour the toggle uniformly

    body = f"""
$secure = ConvertTo-SecureString -String {pwd} -AsPlainText -Force
$newUser = New-ADUser `
    -SamAccountName {sam} `
    -Name {disp} `
    -DisplayName {disp} `
    -GivenName {given} `
    -Surname {sur} `
    -UserPrincipalName {upn} `
    -Path {ou} `
    -AccountPassword $secure `
    -Enabled {enabled_ps} `
    -ErrorAction Stop `
    -PassThru

{groups_section}
{inline_log}

@{{
    status = 'ok'
    action = $__action
    user_dn = $newUser.DistinguishedName
    sam_account_name = $newUser.SamAccountName
}} | ConvertTo-Json -Compress
"""
    return _wrap("create_user", body)


# ---------------------------------------------------------------------------
# modify_group_membership
# ---------------------------------------------------------------------------

def modify_group_membership_script(
    *,
    group_dn: str,
    user_dn: str,
    operation: str,  # "add" | "remove"
    log_in_description: bool,
    action_summary: str,
) -> str:
    if operation not in ("add", "remove"):
        raise ValueError(f"Unsupported operation: {operation!r}")

    cmdlet = "Add-ADGroupMember" if operation == "add" else "Remove-ADGroupMember"
    confirm_flag = "" if operation == "add" else " -Confirm:$false"
    group = _ps_quote(group_dn)
    member = _ps_quote(user_dn)

    # Log on the group object — that's where membership changes matter.
    log_snippet = _description_log_snippet(
        enabled=log_in_description,
        target_dn=group_dn,
        action=f"group_membership_{operation}",
        summary=action_summary,
    )
    body = f"""
{cmdlet} -Identity {group} -Members {member}{confirm_flag} -ErrorAction Stop
{log_snippet}
@{{
    status = 'ok'
    action = $__action
    operation = {_ps_quote(operation)}
    group_dn = {group}
    user_dn = {member}
}} | ConvertTo-Json -Compress
"""
    return _wrap("modify_group_membership", body)
