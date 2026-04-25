# Building Authentication Providers For gSage AI

This document explains how authentication providers work today in gSage AI and
how to add a custom provider that integrates correctly with the login chain,
per-organization configuration, and automatic user synchronization.

The source of truth for this document is:

- `src/shared/auth/base.py`
- `src/shared/auth/registry.py`
- `src/shared/auth/user_sync.py`
- `src/shared/auth/backends/local.py`
- `src/shared/auth/backends/ldap_ad.py`
- `src/backend_api/app/api/v1/auth.py`

---

## Fast Path

If you only need the shortest possible path, do this:

1. Copy `custom_code/auth_backends/example_backend.py` into a new module under `custom_code/auth_backends/`.
2. Create a concrete `BaseAuthProvider` subclass with a unique `name` and `display_name`.
3. Implement `authenticate()` and return a correct `AuthResult`.
4. Define `config_defaults` and `config_schema`.
5. Restart the backend so provider discovery runs.
6. Add the provider name to the organization's `auth_providers` chain and its config to the organization's `auth_config` payload.

There is no manual registry edit.

---

## Core Concepts In Two Minutes

1. Authentication is organization-specific.
   Each organization stores an ordered `auth_providers` list and an encrypted `auth_config` payload.

2. Providers run in a chain.
   `AuthProviderRegistry.authenticate_chain()` tries providers in order until one succeeds or returns a definitive rejection.

3. Built-in providers today are `local` and `ldap`.
   Everything else is an extension.

4. External providers do not create users directly.
   After a successful non-local authentication, the backend calls `upsert_external_user()` to create or update the local user, organization membership, groups, and optional departments.

5. Config is resolved in three layers.
   `config_defaults` < `AUTH_<NAME>__*` environment variables < organization `auth_config[name]`.

6. There is no discovery-time `available=False` gate in the auth provider registry.
   Any concrete `BaseAuthProvider` subclass with a `name` in a scanned package is registered.

---

## Built-In Providers Today

| Provider | Code | Purpose |
|---|---|---|
| `local` | `src/shared/auth/backends/local.py` | Validate the existing local email/password user records |
| `ldap` | `src/shared/auth/backends/ldap_ad.py` | LDAP / Active Directory authentication with group mapping and optional password-expired signaling |

`local` is special because it depends on the login route injecting internal fields such as `_password_hash`, `_email`, and `_full_name` into the provider config before the registry chain runs.

Custom external providers should not rely on that pattern.

---

## Where Providers Live

| Location | Purpose |
|---|---|
| `src/shared/auth/backends/` | Built-in providers maintained by the project |
| `custom_code/auth_backends/` | Operator-provided custom providers |
| `src/shared/auth/base.py` | Provider contract: `BaseAuthProvider`, `AuthResult`, `AuthIdentity`, `AuthErrorType` |
| `src/shared/auth/registry.py` | Auto-discovery and ordered chain execution |
| `src/shared/auth/user_sync.py` | External user upsert, role resolution, group sync, and department sync |

Subdirectories are supported as long as they are Python packages with `__init__.py` files.

---

## Discovery Rules

At backend startup, `AuthProviderRegistry` scans:

- `src.shared.auth.backends`
- `custom_code.auth_backends`

It imports every module under those packages and registers any class that is:

1. a concrete subclass of `BaseAuthProvider`
2. not abstract
3. has a string `name`

Important: unlike MCP tools, auth provider discovery does not currently check an `available` flag.

That means an unfinished concrete provider dropped into `custom_code/auth_backends/` will still be registered.

If you do not want a provider to be discovered yet, keep it outside the scanned package or avoid shipping a concrete subclass with a real `name`.

---

## How The Chain Works

The login flow resolves the organization first, then runs the ordered provider chain from `GSageOrganization.auth_providers`.

Conceptually:

```text
login request
  -> resolve target organization
  -> read org.auth_providers
  -> AuthProviderRegistry.authenticate_chain(...)
       -> provider 1
       -> provider 2
       -> ...
  -> on success:
       local provider: use existing user
       external provider: upsert/sync local user
  -> GuardianKey risk check
  -> issue JWTs
```

Chain behavior is driven by `AuthResult.should_stop_chain`:

- success stops the chain
- `INVALID_CREDENTIALS`, `ACCOUNT_LOCKED`, `ACCOUNT_DISABLED`, and `PASSWORD_EXPIRED` stop the chain
- `USER_NOT_FOUND`, `PROVIDER_UNAVAILABLE`, and `CONFIGURATION_ERROR` allow the next provider to run

---

## Minimal Provider Skeleton

```python
from __future__ import annotations

from typing import ClassVar

from src.shared.auth.base import (
    AuthErrorType,
    AuthIdentity,
    AuthResult,
    BaseAuthProvider,
)


class MyProvider(BaseAuthProvider):
    name: ClassVar[str] = "myprovider"
    display_name: ClassVar[str] = "My Provider"

    config_defaults: ClassVar[dict] = {
        "endpoint": "",
        "timeout_seconds": 10,
    }

    config_schema: ClassVar[dict | None] = {
        "properties": {
            "endpoint": {
                "type": "string",
                "description": "Authentication API base URL",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Request timeout in seconds",
            },
        },
        "required": ["endpoint"],
    }

    async def authenticate(
        self,
        username: str,
        password: str,
        config: dict,
    ) -> AuthResult:
        if not username or not password:
            return AuthResult(
                success=False,
                error_type=AuthErrorType.INVALID_CREDENTIALS,
                error_message="Missing credentials",
            )

        # Replace with your real verification logic.
        success = await verify_against_external_system(username, password, config)

        if not success:
            return AuthResult(
                success=False,
                error_type=AuthErrorType.INVALID_CREDENTIALS,
                error_message="Invalid credentials",
            )

        return AuthResult(
            success=True,
            identity=AuthIdentity(
                email=username,
                full_name=username,
                external_id=f"myprovider:{username}",
            ),
            groups=["external-group-a"],
        )
```

---

## `BaseAuthProvider` Contract

### Required class attributes

| Attribute | Required | Meaning |
|---|---|---|
| `name` | yes | Unique provider identifier used in org config and env var prefixes |
| `display_name` | yes | Human-readable label |

### Optional class attributes

| Attribute | Default | Meaning |
|---|---|---|
| `config_defaults` | `{}` | Lowest-priority config layer |
| `config_schema` | `None` | Schema/metadata for provider config |

### Required method

```python
async def authenticate(
    self,
    username: str,
    password: str,
    config: dict,
) -> AuthResult:
```

The backend will always pass a fully merged config dict.

### Optional methods

| Method | Current status |
|---|---|
| `healthcheck(config)` | Supported by the interface, but not currently called by a public backend/admin flow |
| `change_password(username, old_password, new_password, config)` | Supported by the interface, but not currently wired into the public password-change API |

So you may implement them for completeness, but do not assume the platform invokes them automatically today.

---

## `AuthResult` And `AuthIdentity`

### `AuthIdentity`

| Field | Meaning |
|---|---|
| `email` | Primary email stored locally |
| `full_name` | Display name |
| `external_id` | Stable external identifier, strongly recommended |
| `avatar_url` | Optional profile image URL |
| `phone` | Optional phone number |

Use a stable `external_id` whenever possible. The sync layer prefers `external_id` over email when matching existing users.

### `AuthResult`

| Field | Meaning |
|---|---|
| `success` | Whether authentication succeeded |
| `identity` | Required when `success=True` |
| `groups` | Raw external groups returned by the provider |
| `error_type` | Failure classification used by the chain runner |
| `error_message` | Provider-specific failure detail |
| `must_change_password` | Flows into the login response and JWT claim |
| `extra_claims` | Provider-specific extra data |
| `provider_name` | Set by the registry after the provider returns |

### Error taxonomy and chain behavior

| Error type | Chain behavior | Typical use |
|---|---|---|
| `INVALID_CREDENTIALS` | stop | User exists but password/token is wrong |
| `ACCOUNT_LOCKED` | stop | Account is locked |
| `ACCOUNT_DISABLED` | stop | Account is disabled or not allowed to log in |
| `PASSWORD_EXPIRED` | stop | User must change password |
| `USER_NOT_FOUND` | continue | User does not exist in this provider |
| `PROVIDER_UNAVAILABLE` | continue | Timeout, network failure, provider outage |
| `CONFIGURATION_ERROR` | continue | Bad config or missing mandatory config |

Be careful with `USER_NOT_FOUND`: use it only when you truly want the next provider in the chain to get a chance.

---

## Config Layers And Environment Variables

Provider config is merged in this order:

1. `config_defaults`
2. `AUTH_<NAME>__<FIELD>` environment variables
3. organization `auth_config[provider_name]`

That means the highest-priority values come from the organization's encrypted config blob.

### Environment variable naming

For a provider named `ldap`:

```env
AUTH_LDAP__SERVER_URL=ldaps://dc.example.com:636
AUTH_LDAP__BIND_DN=CN=svc-auth,OU=Service Accounts,DC=example,DC=com
```

### Updating `.env.example`

After adding or changing configurable providers, regenerate the auth defaults zone:

```bash
python scripts/generate_env_defaults.py
```

---

## Organization Configuration Shape

The organization model stores auth settings in two places:

- `auth_providers`: ordered JSON list of provider names
- `auth_config`: encrypted JSON object keyed by provider name

Example:

```json
{
  "auth_providers": ["ldap", "local"],
  "auth_config": {
    "ldap": {
      "server_url": "ldaps://dc.example.com:636",
      "bind_dn": "CN=svc-auth,OU=Service Accounts,DC=example,DC=com",
      "bind_password": "...",
      "user_search_base": "OU=Users,DC=example,DC=com"
    }
  }
}
```

Provider names in `auth_providers` must match the provider class `name` exactly.

---

## Automatic User, Group, And Department Sync

For successful non-local authentication, the backend calls `upsert_external_user()`.

The current behavior is:

1. find existing user by `external_id`
2. fall back to `email`
3. create the user if missing
4. ensure an org membership row exists
5. resolve role from `group_mapping`
6. sync local groups from external groups
7. sync optional departments from external groups

Your provider should therefore return:

- a stable `AuthIdentity`
- raw external group identifiers in `AuthResult.groups`

### Supported `provider_config` keys used by sync logic

The sync layer understands these keys today:

| Key | Meaning |
|---|---|
| `group_mapping` | Map external group identifiers to local role/groups/department |
| `default_role` | Fallback role if no mapping entry applies |
| `auto_create_groups` | Create missing local groups on demand |
| `auto_create_departments` | Create missing departments on demand |

Each `group_mapping` entry may contain:

```json
{
  "role": "member",
  "groups": ["soc-analysts"],
  "department": "Tier 1"
}
```

Role resolution is priority-based: `owner` > `admin` > `member` > `viewer`.

If no department mapping is found at all, the sync layer places the user in the organization's default department.

Important: mapping keys must match the raw group identifiers returned by your provider exactly.

---

## Local Provider Special Case

The built-in `local` provider does not talk to an external system. It is a thin wrapper over the existing local user database.

The login route injects these internal keys into the provider config before running it:

- `_password_hash`
- `_email`
- `_full_name`

This is a local-provider implementation detail, not a general contract for custom providers.

Also note:

- local users change passwords through the existing backend password-change route
- external providers are not currently wired into that route even though `BaseAuthProvider` exposes a `change_password()` hook

---

## `must_change_password`

Providers may set `AuthResult.must_change_password = True`.

The backend currently propagates that flag in two places:

- the login response body
- the JWT payload as `pwd_change_required`

The built-in LDAP provider uses this for AD `pwdLastSet = 0` scenarios.

---

## GuardianKey Is Not An Auth Provider

GuardianKey adaptive authentication is a separate post-credential risk check in the login flow.

That means:

- it runs after the provider chain validates credentials
- it can allow, notify, or block access
- it is not implemented as a `BaseAuthProvider`

If you are building an identity provider, focus on the provider contract in this document. Do not try to model post-auth risk scoring as an auth provider unless the application architecture changes.

---

## Common Pitfalls

1. Do not leave unfinished concrete providers inside `custom_code/auth_backends/`.
   They will be discovered.

2. Do not return `USER_NOT_FOUND` when the user exists but the password is wrong.
   That would incorrectly allow fall-through to the next provider.

3. Do not provision users manually inside the provider.
   Let `user_sync` do it.

4. Do not transform groups into local names inside the provider.
   Return raw external group identifiers and let `group_mapping` translate them.

5. Do not assume `healthcheck()` or `change_password()` are automatically invoked today.

6. Do not forget that provider names are configuration keys.
   Renaming a provider changes env prefixes and org config keys.

---

## Rollout Checklist

1. Add the provider module under `custom_code/auth_backends/`.
2. Add `__init__.py` files for any new subpackages.
3. Define `name`, `display_name`, `config_defaults`, and `config_schema`.
4. Implement `authenticate()` and return correct `AuthResult` values.
5. Run `python scripts/generate_env_defaults.py` if you added config fields.
6. Restart the backend.
7. Add the provider to the organization's `auth_providers` list.
8. Add the provider config under the matching key in the organization's `auth_config`.
9. Test chain behavior for success, invalid credentials, user-not-found, and provider outage cases.