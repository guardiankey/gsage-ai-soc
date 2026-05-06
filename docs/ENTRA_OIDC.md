# Microsoft Entra OIDC Setup

This guide explains the Microsoft-side setup required for gSage to use Microsoft Entra ID as an OIDC SSO provider. It complements the operational auth-provider documentation and focuses on what an operator or tenant administrator must configure in Microsoft.

## What gSage expects

gSage uses the Microsoft identity platform with the authorization code flow, PKCE, and a dedicated callback per organization.

At a minimum, each gSage organization needs:

- one Entra app registration
- its Application (client) ID
- its Directory (tenant) ID
- one client secret value
- one web redirect URI that points back to gSage

By default, if no explicit redirect URI is stored in the provider config, gSage derives it as:

```text
https://<public-base-url>/api/v1/auth/sso/<org_slug>/entra_oidc/callback
```

The `<public-base-url>` must match the value configured in gSage as `public_base_url`, and `<org_slug>` must be the exact organization slug used in gSage.

gSage can also use Entra groups for two separate purposes:

- a login gate through `required_groups`
- role and department mapping through `group_mapping`

Important: gSage maps Entra group object IDs, not group display names.

## Recommended Entra design

For most internal deployments, the safest default is:

- supported account type: `Accounts in this organizational directory only`
- one tenant per gSage organization
- security groups for authorization

Use multi-tenant registration only when you intentionally want the same gSage integration to accept users from more than one Entra tenant.

## Microsoft-side setup

### 1. Register the application

In Microsoft Entra admin center:

1. Go to `Microsoft Entra ID -> App registrations -> New registration`.
2. Choose a clear name such as `gSage SSO - <org name>`.
3. Select the supported account type.
4. Register the app.
5. Save these values from the Overview page:
   - `Application (client) ID`
   - `Directory (tenant) ID`

Recommended supported account type:

- `Accounts in this organizational directory only`

### 2. Add the redirect URI

Under the app registration:

1. Open `Authentication`.
2. Add a platform: `Web`.
3. Add the exact gSage callback URI.

Example:

```text
https://gsage.example.com/api/v1/auth/sso/acme/entra_oidc/callback
```

The value must match exactly. Any difference in scheme, host, path, or trailing slash causes redirect mismatch errors.

### 3. Create a client secret

Under `Certificates & secrets`:

1. Create a new client secret.
2. Copy the secret value immediately.

Only the secret value is useful to gSage. The secret ID is not enough.

### 4. Review API permissions

gSage defaults to these scopes for the Entra OIDC provider:

```text
openid profile email User.Read
```

Practical meaning:

- `openid`, `profile`, and `email` support the OIDC identity flow
- `User.Read` is the default Microsoft Graph delegated permission used by gSage

This matters for group resolution. When Entra includes the `groups` claim directly in the token, gSage reads it from the token. When the user belongs to too many groups and Entra emits an overage marker instead, gSage falls back to `POST /me/getMemberObjects`. Microsoft documents `User.Read` as the least-privileged delegated permission for the signed-in user's memberships on that endpoint.

In strict tenants, you may still prefer to grant admin consent in advance so users are not blocked by tenant consent policy.

### 5. Configure group claims

If you want to drive access from Entra groups:

1. Open `Token configuration` in the app registration.
2. Add a groups claim.
3. Choose the group source that matches your governance model.

Recommended choices:

- `Security groups` when you use dedicated security groups for gSage access
- `Groups assigned to the application` when you want tighter scoping and smaller tokens

Microsoft documents a `groups` claim size limit of 200 groups in JWTs. When the user exceeds that limit, Entra stops sending the full `groups` array and emits an overage indicator instead. gSage handles this by calling Microsoft Graph for the signed-in user.

Operational advice:

- if possible, prefer `Groups assigned to the application` to reduce token size
- keep a record of the Entra group object IDs you plan to use in `required_groups` and `group_mapping`
- do not map by display name, because display names can change

### 6. Optional: assign users or groups to the enterprise app

If you use `Groups assigned to the application`, you must also assign the relevant users or groups to the enterprise application. Otherwise, users can authenticate successfully in Microsoft but still not receive the expected groups claim.

### 7. Optional: app roles

Microsoft app roles are useful for Entra-side governance, but gSage currently authorizes Entra OIDC users primarily through Entra group IDs. If you choose to define app roles, treat them as a Microsoft governance layer, not as a replacement for gSage `group_mapping` unless you also change the gSage authorization model.

## gSage-side configuration

The provider must also be enabled inside gSage.

### 1. Set the auth-provider chain

Example:

```bash
python -m ops_cli auth-providers set \
  --org-slug acme \
  --providers entra_oidc,local
```

This means gSage will expose Entra OIDC SSO for the organization while still keeping local auth in the chain.

### 2. Configure the Entra OIDC provider

Example:

```bash
echo '<client-secret>' | python -m ops_cli auth-providers config \
  --org-slug acme \
  --provider entra_oidc \
  --client-id 11111111-1111-1111-1111-111111111111 \
  --tenant-id 22222222-2222-2222-2222-222222222222 \
  --client-secret-stdin \
  --default-role viewer
```

Optional fields supported by gSage include:

- `redirect_uri`: override the auto-derived callback if you need an exact explicit value
- `scopes`: override the default OIDC scopes
- `required_groups`: block login unless the user belongs to at least one allowed Entra group
- `group_mapping`: map Entra group object IDs to gSage roles, groups, and departments
- `auto_create_groups`: create missing local gSage groups automatically
- `auto_create_departments`: create missing local departments automatically

### 3. Add email-domain lookup mappings when needed

If your login flow uses organization discovery from the user's email domain, add the domain mapping too:

```bash
python -m ops_cli auth-providers domain add \
  --org-slug acme \
  --domain acme.com
```

### 4. Manage group mapping safely

For anything larger than a trivial mapping, use round-trip editing:

```bash
python -m ops_cli auth-providers get-mapping \
  --org-slug acme \
  --provider entra_oidc > mapping.json
```

Edit `mapping.json`, then apply it:

```bash
python -m ops_cli auth-providers config \
  --org-slug acme \
  --provider entra_oidc \
  --group-mapping-stdin < mapping.json
```

Example mapping:

```json
{
  "33333333-3333-3333-3333-333333333333": {
    "role": "member",
    "groups": ["soc-analysts"],
    "departments": ["Security Ops"],
    "dept_role": "member"
  },
  "44444444-4444-4444-4444-444444444444": {
    "role": "admin"
  }
}
```

## Troubleshooting

### `AADSTS50011` or redirect mismatch

The redirect URI configured in Entra does not exactly match the callback URI gSage is using.

Check:

- scheme: `https` vs `http`
- correct public hostname
- exact org slug
- no extra slash at the end
- no stale override in `redirect_uri`

### Sign-in succeeds in Microsoft but gSage still blocks the user

Common causes:

- the user is not in any configured `required_groups`
- the wrong group object ID was copied into `required_groups` or `group_mapping`
- the app registration is not configured to emit group claims
- the user or group was not assigned to the enterprise app when using `Groups assigned to the application`

### Group mapping does not apply

Check whether you used the Entra group display name instead of the group object ID. gSage expects the object ID.

### Large group memberships behave inconsistently

This usually means group overage is happening. Confirm that:

- the app still has the default delegated `User.Read` permission
- Entra is configured to emit group claims
- tenant consent policy is not blocking the Graph call for the signed-in user

### Users from the wrong tenant can log in

Review both:

- the app registration supported account type
- the `tenant_id` stored in gSage

For single-tenant deployments, do not use `common` unless you intentionally want cross-tenant behavior.

## Official Microsoft references

- Register an application in Microsoft Entra ID: <https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app>
- Configure a web app sign-in flow and redirect URIs: <https://learn.microsoft.com/en-us/entra/identity-platform/scenario-web-app-sign-user-app-registration>
- Configure group claims and app roles in tokens: <https://learn.microsoft.com/en-us/security/zero-trust/develop/configure-tokens-group-claims-app-roles>
- Microsoft Graph `getMemberObjects`: <https://learn.microsoft.com/en-us/graph/api/directoryobject-getmemberobjects?view=graph-rest-1.0>
