
# Scripts Directory

This directory contains maintenance and bootstrap utilities for the current
gSage AI stack.

## At a Glance

| Script | What it does | Main parameters |
|---|---|---|
| `generate_env_defaults.py` | Regenerates the auto-generated tool and auth provider sections in `.env.example` | `--dry-run` |
| `generate_tools_docs.py` | Regenerates operator-facing Markdown docs under `docs/tools/` plus a derived `.env.tools.example` | `--actions-depth`, `--check`, `--only`, `--output-dir`, `--env-file` |
| `get_admin.py` | Retrieves or creates the bootstrap admin API key and can also reset the bootstrap admin password | `--reset-password [NEW_PASSWORD]` |
| `init-elasticsearch.py` | Creates Elasticsearch ILM policies and index templates used by the application | None |
| `manage_otp.py` | Lists or changes OTP / TOTP state for users directly in the database | `list`, `disable`, `reset`, `clear-devices` |
| `manage_users.py` | Creates users, inspects memberships, resets passwords, updates attributes, and adds users to groups | `create`, `list`, `info`, `reset-password`, `update`, `add-to-group` |

## General Notes

- Run these commands from the repository root.
- Most scripts import the application directly and therefore expect the usual environment variables to be available.
- Database-facing scripts require the application database configuration to be valid.
- These scripts are operational utilities. Review output carefully before using them in production.

## Detailed Usage

### `generate_env_defaults.py`

Regenerates the auto-generated sections in `.env.example` for:

- MCP tool configuration defaults
- authentication provider configuration defaults

The script scans the current tool and auth provider packages, rebuilds the two
generated zones, and writes them back into `.env.example`.

### Usage

```bash
python scripts/generate_env_defaults.py
python scripts/generate_env_defaults.py --dry-run
```

### Parameters

| Parameter | Required | Description |
|---|---|---|
| `--dry-run` | No | Prints the generated TOOL and AUTH sections to stdout instead of writing `.env.example` |

### Notes

- This script does not use `argparse`; it only checks whether `--dry-run` is present in `sys.argv`.
- Use it after adding or changing tool `config_defaults` / `config_schema` or auth provider config declarations.
- If the expected markers are missing from `.env.example`, the script warns and skips that section.

### `generate_tools_docs.py`

Regenerates operator-facing Markdown documentation for every MCP tool. Output:

- One Markdown file per tool group under `docs/tools/`. Tools sharing a
  `config_namespace` (e.g. all Trellix EDR tools) are merged into a single
  page; standalone tools get their own page next to the source folder.
- An index `docs/tools/README.md` listing every group.
- A consolidated `.env.tools.example` at the repo root with every
  configuration-derived environment variable. Sensitive values are
  emitted with the placeholder `__SET_ME__` and a security comment.

The source of truth is each tool's Python source — ClassVars, `config_schema`,
`config_defaults`, `params_schema`, plus module/class docstrings. Generated
files carry an auto-generated banner and **must not be edited by hand**.

### Usage

```bash
python scripts/generate_tools_docs.py
python scripts/generate_tools_docs.py --actions-depth medium
python scripts/generate_tools_docs.py --check               # CI guard
python scripts/generate_tools_docs.py --only "trellix.*"   # debug
```

### Parameters

| Parameter | Required | Description |
|---|---|---|
| `--actions-depth {brief,medium}` | No | Detail level for action-driven tools. `brief` (default) lists actions only; `medium` adds a per-action parameter table |
| `--output-dir PATH` | No | Override the output directory (default: `docs/tools/`) |
| `--env-file PATH` | No | Override the consolidated env-vars file (default: `.env.tools.example`) |
| `--only REGEX` | No | Restrict generation to tools whose name matches the regex |
| `--check` | No | Dry-run mode. Exits with status `1` if any output would change — useful for CI guards |

### Notes

- Idempotent — running twice in a row produces zero diff.
- Reuses the discovery helper from `generate_env_defaults.py`, so it picks up
  exactly the same set of tools.
- Run it after adding/removing tools, changing `config_schema`, `permissions`,
  `params_schema`, or any docstring intended for operators.

### `get_admin.py`

Retrieves the bootstrap admin API key. The script first scans `docker compose`
logs for a previously printed key. If that fails, it connects to the database to
create or rotate the bootstrap key. It can also reset the bootstrap admin
password.

### Usage

```bash
python scripts/get_admin.py
python scripts/get_admin.py --reset-password
python scripts/get_admin.py --reset-password "MyNewStrongPassword"
```

### Parameters

| Parameter | Required | Description |
|---|---|---|
| `--reset-password [NEW_PASSWORD]` | No | Resets the bootstrap admin password. If the value is omitted, the script generates a secure random password. If a value is provided, that value is used as the new password |

### Notes

- Without parameters, the script tries to recover the existing admin API key before creating or rotating one.
- Output includes ready-to-export environment variables such as `GSAGE_API_KEY`, `GSAGE_ORG_ID`, `GSAGE_DEPT_ID`, and `GSAGE_API_HOST` when available.
- If bootstrap is disabled because `ADMIN_EMAIL` is not configured, the script prints a warning instead of creating credentials.

### `init-elasticsearch.py`

Initializes the Elasticsearch side of the platform by checking cluster health,
creating ILM policies, and creating index templates.

### Usage

```bash
python scripts/init-elasticsearch.py
```

### Parameters

This script has no CLI parameters.

### Notes

- The script exits with a non-zero status if Elasticsearch is unhealthy or initialization fails.
- It depends on the application's Elasticsearch client configuration being available in the environment.

### `manage_otp.py`

Administrative utility for OTP / TOTP user settings. It connects directly to
the database and supports read-only inspection plus remediation actions.

### Usage

```bash
python scripts/manage_otp.py list
python scripts/manage_otp.py list --org my-org
python scripts/manage_otp.py disable user@example.com
python scripts/manage_otp.py reset user@example.com
python scripts/manage_otp.py clear-devices user@example.com
```

### Commands and parameters

#### `list`

Lists users and their OTP status.

| Parameter | Required | Description |
|---|---|---|
| `--org SLUG` | No | Filters the listing by organization slug |

#### `disable`

Soft-disables OTP for a user.

| Parameter | Required | Description |
|---|---|---|
| `email` | Yes | User email address |

Behavior: clears `otp_enabled` and `otp_confirmed_at`, but keeps the secret,
backup codes, and trusted devices.

#### `reset`

Fully resets OTP for a user.

| Parameter | Required | Description |
|---|---|---|
| `email` | Yes | User email address |

Behavior: disables OTP, clears the encrypted OTP secret, clears backup codes,
and removes trusted devices.

#### `clear-devices`

Deletes all trusted devices for a user without changing the OTP secret itself.

| Parameter | Required | Description |
|---|---|---|
| `email` | Yes | User email address |

### Notes

- This script requires the normal application environment variables, including database access and encryption-related settings.
- Organization filtering uses the organization slug.

### `manage_users.py`

Administrative user management CLI for local users and organization
memberships.

### Usage

```bash
python scripts/manage_users.py create --email alice@example.com --org myorg --role member
python scripts/manage_users.py list --org myorg
python scripts/manage_users.py info --email alice@example.com
python scripts/manage_users.py reset-password --email alice@example.com
python scripts/manage_users.py update --email alice@example.com --full-name "Alice Smith"
python scripts/manage_users.py add-to-group --email alice@example.com --org myorg --group devs
```

### Common value rules

- `--org` accepts organization UUID, slug, or name depending on the command.
- `--role` accepts one of: `owner`, `admin`, `member`, `viewer`.
- When a password argument is omitted on creation or reset, the script generates a secure random password.

### Commands and parameters

#### `create`

Creates a user if needed and ensures membership in an organization.

| Parameter | Required | Description |
|---|---|---|
| `--email` | Yes | User email |
| `--org` | Yes | Organization UUID, slug, or name |
| `--full-name` | No | Full name. Defaults to the local-part of the email for new users |
| `--password` | No | Initial password. If omitted for a new user, a password is generated |
| `--role` | No | Membership role. Default: `member` |

#### `list`

Lists members of an organization.

| Parameter | Required | Description |
|---|---|---|
| `--org` | Yes | Organization UUID, slug, or name |

#### `info`

Shows details about a user, including memberships and groups.

| Parameter | Required | Description |
|---|---|---|
| `--email` | Yes | User email |

#### `reset-password`

Resets a user's password.

| Parameter | Required | Description |
|---|---|---|
| `--email` | Yes | User email |
| `--password` | No | New password. If omitted, a password is generated |

#### `update`

Updates user attributes and optionally updates the role within an organization.

| Parameter | Required | Description |
|---|---|---|
| `--email` | Yes | User email |
| `--full-name` | No | New full name |
| `--active true\|false` | No | Sets whether the user is active. Accepted truthy values include `1`, `true`, `yes`, and `sim` |
| `--ai-instructions` | No | Custom AI instructions. Use an empty string to remove the stored value |
| `--role` | No | New organization role |
| `--org` | Conditionally | Required when `--role` is provided |

#### `add-to-group`

Adds a user to a group within an organization.

| Parameter | Required | Description |
|---|---|---|
| `--email` | Yes | User email |
| `--org` | Yes | Organization UUID, slug, or name |
| `--group` | Yes | Group name or UUID |

### Notes

- `create` also adds the user to the organization's default department when one exists.
- `add-to-group` requires the user to already be a member of the target organization.

