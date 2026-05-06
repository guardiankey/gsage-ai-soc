# Microsoft 365 and Exchange Online Email Setup

This guide complements `docs/channels/email.md` and focuses on the Microsoft-side setup required when gSage uses Microsoft 365 or Exchange Online mailboxes through OAuth2 app-only authentication.

## What gSage expects

The Microsoft 365 integration in gSage uses OAuth2 client credentials with XOAUTH2 for both IMAP and SMTP.

In practice, that means the email account in gSage must be configured with:

- `auth_method = oauth2`
- Entra tenant ID
- Entra client ID
- Entra client secret

If you leave the advanced fields empty, gSage defaults to:

- token endpoint:

```text
https://login.microsoftonline.com/<tenant_id>/oauth2/v2.0/token
```

- scope:

```text
https://outlook.office365.com/.default
```

This flow is app-only. No end-user login is involved.

## Recommended Microsoft design

For most deployments:

- register the app as single-tenant
- grant only the Exchange Online application permissions required for IMAP and SMTP
- explicitly authorize the application against the specific mailbox or shared mailbox gSage will use

This is more predictable than trying to reuse a broad multi-tenant app.

## Microsoft-side setup

### 1. Register the Entra application

In Microsoft Entra admin center:

1. Go to `Microsoft Entra ID -> App registrations -> New registration`.
2. Choose a name such as `gSage Email - <org name>`.
3. Prefer `Accounts in this organizational directory only`.
4. Register the app.
5. Save:
   - `Application (client) ID`
   - `Directory (tenant) ID`

### 2. Add Exchange Online application permissions

In `API permissions`:

1. Select `Add a permission`.
2. Choose `APIs my organization uses`.
3. Search for `Office 365 Exchange Online`.
4. Add these application permissions:
   - `IMAP.AccessAsApp`
   - `SMTP.SendAsApp`

Important:

- these are Exchange Online application permissions
- Microsoft Graph mail permissions are not a substitute for IMAP and SMTP protocol access

### 3. Grant admin consent

After adding the permissions, grant admin consent for the tenant.

Without admin consent, token issuance may work in some flows, but Exchange Online access will not be authorized correctly for the app-only protocol path.

### 4. Create a client secret

Under `Certificates & secrets`:

1. Create a new client secret.
2. Copy the secret value immediately.

This value is the `oauth_client_secret` stored in gSage.

### 5. Register the service principal in Exchange Online

This is the step many operators miss. Entra permissions alone are not enough for mailbox access through IMAP and SMTP app-only authentication.

Use Exchange Online PowerShell:

```powershell
Connect-ExchangeOnline -Organization <tenant>.onmicrosoft.com
New-ServicePrincipal -AppId <APPLICATION_ID> -ObjectId <ENTERPRISE_APPLICATION_OBJECT_ID>
```

Important:

- use the enterprise application object ID for the service principal registration
- do not use the app registration object ID by mistake
- if you use the wrong object ID, authentication failures can be hard to diagnose later

### 6. Grant mailbox access to the Exchange service principal

Grant mailbox access for each mailbox gSage should read:

```powershell
Add-MailboxPermission -Identity soc-mailbox@example.com -User <EXCHANGE_SERVICE_PRINCIPAL_ID> -AccessRights FullAccess
```

If SMTP sending still fails in your tenant, also grant Send As:

```powershell
Add-RecipientPermission -Identity soc-mailbox@example.com -Trustee <EXCHANGE_SERVICE_PRINCIPAL_ID> -AccessRights SendAs
```

Why both may matter:

- `FullAccess` is commonly required for mailbox access
- Microsoft explicitly calls out `Add-RecipientPermission` for client-credentials Send As scenarios

### 7. Confirm SMTP AUTH is enabled where needed

SMTP AUTH may be disabled at tenant or mailbox level even when OAuth2 is configured correctly.

Tenant-wide check:

```powershell
Get-TransportConfig | Format-List SmtpClientAuthenticationDisabled
```

Per-mailbox check:

```powershell
Get-CASMailbox -Identity soc-mailbox@example.com | Format-List SmtpClientAuthenticationDisabled
```

To enable SMTP AUTH for a mailbox when required:

```powershell
Set-CASMailbox -Identity soc-mailbox@example.com -SmtpClientAuthenticationDisabled $false
```

Operational note:

- security defaults can disable SMTP AUTH in some tenants
- mailbox settings override the tenant-wide setting

## gSage-side configuration

The current gSage setup path for this OAuth2 model is the Admin UI email account form.

In the gSage email account configuration:

1. Choose `Authentication Method = OAuth2`.
2. Fill in:
   - `Tenant ID`
   - `Client ID`
   - `Client Secret`
3. Leave `OAuth Token Endpoint` empty unless you intentionally need a non-default cloud endpoint.
4. Leave `OAuth Scope` empty unless you intentionally need to override the default.
5. Save the account.
6. Run `Test Connection`.

Practical field guidance:

- the mailbox address configured in gSage should be the mailbox actually accessed in Exchange Online
- the IMAP username should be the mailbox UPN or mailbox email address, not the application name
- if you use a shared mailbox, configure the shared mailbox address and grant permissions to that mailbox explicitly

## Validation checklist

Before declaring the integration ready, confirm:

- the app registration exists in the correct tenant
- the app has `IMAP.AccessAsApp` and `SMTP.SendAsApp`
- admin consent was granted
- the Exchange service principal was created successfully
- the mailbox has `FullAccess`
- the mailbox has `SendAs` if your SMTP path requires it
- SMTP AUTH is enabled where needed
- gSage email account is set to `OAuth2`
- gSage `Test Connection` succeeds for both IMAP and SMTP

## Troubleshooting

### Token request fails immediately

Typical causes:

- wrong tenant ID
- wrong client ID
- expired or rotated client secret
- app registered in a different tenant than the mailbox tenant

### IMAP login fails even though a token is issued

Common causes:

- `IMAP.AccessAsApp` permission missing
- admin consent not granted
- Exchange service principal not created
- mailbox `FullAccess` not granted to the Exchange service principal
- username points to the wrong mailbox

### SMTP login or send fails even though IMAP works

Usually one of these is the issue:

- `SMTP.SendAsApp` permission missing
- SMTP AUTH disabled for the tenant or mailbox
- `SendAs` permission still missing in Exchange Online
- SMTP sender address does not match the mailbox you authorized

### The permission picker shows Graph mail permissions but not Exchange protocol permissions

Do not use Graph `Mail.Read`, `Mail.Send`, or similar permissions as a replacement. For gSage's IMAP and SMTP protocol flow, the relevant permissions are on `Office 365 Exchange Online`, not Microsoft Graph.

### The app is multi-tenant or centrally registered by a partner

In that scenario, admin consent must still happen in the resource tenant, and Exchange service principal registration must still be performed in that tenant. If possible, keep the deployment single-tenant to reduce ambiguity.

## Official Microsoft references

- Authenticate an IMAP, POP, or SMTP connection using OAuth: <https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth>
- Enable or disable authenticated client SMTP submission in Exchange Online: <https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission>
- Register an application in Microsoft Entra ID: <https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app>
