# Admin Scripts

Standalone CLI scripts for database administration and maintenance tasks.  
All scripts require the application environment variables to be set (e.g. `DATABASE_URL`, `ENCRYPTION_KEY`). They connect directly to the database and do **not** go through the HTTP API.

> **Prerequisites:** Activate the virtual environment before running any script.
>
> ```bash
> source .venv/bin/activate
> ```

---

## `scripts/get_admin.py`

Retrieves or creates the bootstrap admin API key.

### Workflow

1. Searches `docker compose logs backend_api` for a previously printed raw key.
2. If found → prints it.
3. If not found → connects to the database:
   - Admin user does not exist → seeds the admin (`ensure_admin`) → prints the new key.
   - Admin user exists but the raw key is gone → rotates the key (revokes old, creates new) → prints the new key.

### Usage

```bash
# Get or create the admin API key
python scripts/get_admin.py

# Reset the admin password (auto-generates a secure password)
python scripts/get_admin.py --reset-password

# Reset the admin password with a specific value
python scripts/get_admin.py --reset-password "MyNewPassword123!"
```

### Output

The script prints the API key and the environment variable exports needed to use the CLI client:

```
export GSAGE_API_KEY="gk_live_..."
export GSAGE_ORG_ID="<uuid>"
export GSAGE_API_HOST="http://localhost:8000"
```

### Notes

- If `ADMIN_PASSWORD` is not set in the environment, a secure password is auto-generated and printed once. Store it safely.
- Key format: `gk_live_<base64url>` or `gk_test_<base64url>`.

---

## `scripts/manage_otp.py`

Admin utility for managing OTP (TOTP 2FA) settings per user.

### Subcommands

| Subcommand | Description |
|---|---|
| `list` | List all users and their OTP status |
| `disable` | Soft-disable 2FA (keeps secret, clears `otp_enabled` flag) |
| `reset` | Fully reset 2FA (clears secret, backup codes, and trusted devices) |
| `clear-devices` | Delete all trusted devices for a user |

### Usage

```bash
# List all users and their OTP status
python scripts/manage_otp.py list

# Filter by organization slug
python scripts/manage_otp.py list --org my-org

# Disable 2FA for a user (soft disable — keeps the TOTP secret)
python scripts/manage_otp.py disable user@example.com

# Fully reset 2FA for a user (removes secret, backup codes, trusted devices)
python scripts/manage_otp.py reset user@example.com

# Delete all trusted devices for a user
python scripts/manage_otp.py clear-devices user@example.com
```

### Notes

- `disable` vs `reset`: use `disable` for a temporary lock; use `reset` when the user needs to re-enroll from scratch (e.g. lost authenticator app).
- The `list` command uses [Rich](https://github.com/Textualize/rich) to render a formatted table in the terminal.

---

## `scripts/manage_users.py`

User management CLI for creating, updating, and inspecting users and their organization memberships.

### Subcommands

| Subcommand | Description |
|---|---|
| `create` | Create a user and add them to an org |
| `list` | List members of an org |
| `info` | Show full details for a user (orgs, groups, OTP status) |
| `reset-password` | Reset a user's password |
| `update` | Update user attributes (name, status, role, AI instructions) |
| `add-to-group` | Add a user to a group within an org |

> **Note:** The `--org` argument accepts a UUID, slug, or name (case-insensitive) for all subcommands.

### Usage

```bash
# Create a user and add to an org (password auto-generated if omitted)
python scripts/manage_users.py create \
  --email alice@example.com \
  --org myorg \
  --full-name "Alice Smith" \
  --role member

# Create with an explicit password
python scripts/manage_users.py create \
  --email alice@example.com \
  --org myorg \
  --password "SecurePass123!"

# List all members of an org
python scripts/manage_users.py list --org myorg

# Show user details (orgs, groups, OTP status, AI instructions)
python scripts/manage_users.py info --email alice@example.com

# Reset password (auto-generates if --password is omitted)
python scripts/manage_users.py reset-password --email alice@example.com
python scripts/manage_users.py reset-password --email alice@example.com --password "NewPass456!"

# Update user attributes
python scripts/manage_users.py update --email alice@example.com --full-name "Alice B. Smith"
python scripts/manage_users.py update --email alice@example.com --active false
python scripts/manage_users.py update --email alice@example.com --role admin --org myorg
python scripts/manage_users.py update --email alice@example.com --ai-instructions "Always respond in English."

# Remove AI instructions
python scripts/manage_users.py update --email alice@example.com --ai-instructions ""

# Add user to a group within an org
python scripts/manage_users.py add-to-group \
  --email alice@example.com \
  --org myorg \
  --group devs
```

### Valid Roles

`owner` | `admin` | `member` | `viewer`

### Notes

- `create` is idempotent regarding the user record: if the user already exists globally, they are simply added to the specified org (no duplicate user is created).
- `update --role` requires `--org` to identify which org membership to update.
- `add-to-group` requires the user to already be a member of the org.
- `--group` accepts either a group name (case-insensitive) or a UUID.

---

## `scripts_operations/publish-images.sh`

Builds and pushes the production Docker images to a registry.

Defaults read the release tag from the `./VERSION` file at the repo root.
Default targets are `backend_api`, `worker_tools`, `mcp_server`, `frontend`,
and `curator`. Images are named `<registry>/gsage-<target>:<tag>` (e.g.
`guardiankey/gsage-backend_api:0.1.0`).

```bash
# Dry-run (prints the docker commands it would execute)
scripts_operations/publish-images.sh --dry-run

# Publish every baseline image at the version from ./VERSION to Docker Hub
scripts_operations/publish-images.sh --registry guardiankey --push

# Publish a single image
scripts_operations/publish-images.sh -t backend_api --tag 0.1.0 --push
```

---

## `scripts_operations/build-release-bundle.sh`

Assembles the self-contained installer tarball consumed by operators.

Requires that `publish-images.sh` has already pushed the images for the given
version, because the bundle builder calls `docker manifest inspect` to record
each image digest into `MANIFEST.json`.

```bash
# Build dist/gsage-0.1.0.tar.gz using guardiankey/gsage-*:0.1.0 images
scripts_operations/build-release-bundle.sh --version 0.1.0 --registry guardiankey

# Stage only, do not produce the tarball
scripts_operations/build-release-bundle.sh --version 0.1.0 --registry guardiankey --dry-run
```

The resulting bundle is what customers download and run:

```bash
tar -xzf gsage-0.1.0.tar.gz
sudo bash gsage-0.1.0/installer.sh
```

See [docs-local/architecture/50-INSTALLER.md](../docs-local/architecture/50-INSTALLER.md)
for the full installer design.
