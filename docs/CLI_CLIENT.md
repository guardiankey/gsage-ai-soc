# gSage AI — CLI Client

Developer reference for the two command-line tools shipped with the project.

---

## Table of Contents

- [gSage AI — CLI Client](#gsage-ai--cli-client)
  - [Table of Contents](#table-of-contents)
  - [Installation](#installation)
  - [Admin Console (TUI)](#admin-console-tui)
    - [Requirements](#requirements)
    - [Launching](#launching)
    - [Keyboard shortcuts](#keyboard-shortcuts)
    - [Navigation](#navigation)
  - [Authentication](#authentication)
    - [API Key (recommended)](#api-key-recommended)
    - [Email + Password](#email--password)
    - [Interactive (REPL only)](#interactive-repl-only)
  - [Interactive REPL](#interactive-repl)
    - [Authentication commands](#authentication-commands)
    - [Two-Factor Authentication (OTP) commands](#two-factor-authentication-otp-commands)
    - [Conversation commands](#conversation-commands)
    - [Knowledge base commands](#knowledge-base-commands)
    - [Approval commands](#approval-commands)
    - [File commands](#file-commands)
    - [Other commands](#other-commands)
    - [Tab completion](#tab-completion)
  - [Batch Document Ingest](#batch-document-ingest)
    - [Usage](#usage)
    - [Options](#options)
    - [Examples](#examples)
    - [Sample output](#sample-output)
    - [Exit codes](#exit-codes)
  - [Scheduled Jobs (REPL)](#scheduled-jobs-repl)
    - [List jobs](#list-jobs)
    - [Show job details](#show-job-details)
    - [Create a PROMPT\_RUN job](#create-a-prompt_run-job)
    - [Activate / Deactivate](#activate--deactivate)
    - [Delete](#delete)
  - [Approval Rules (REPL)](#approval-rules-repl)
    - [List rules](#list-rules)
    - [Show rule details](#show-rule-details)
    - [Create a rule](#create-a-rule)
    - [Update a rule](#update-a-rule)
    - [Activate / Deactivate](#activate--deactivate-1)
    - [Delete](#delete-1)
  - [DataStores (REPL)](#datastores-repl)
    - [List stores](#list-stores)
    - [Show store details](#show-store-details)
    - [Create a store](#create-a-store)
    - [Update a store](#update-a-store)
    - [Delete a store](#delete-a-store)
    - [List records](#list-records)
    - [Show a single record](#show-a-single-record)
    - [Add a record](#add-a-record)
    - [Update a record](#update-a-record)
    - [Delete a record](#delete-a-record)
    - [Query records](#query-records)
  - [Departments (REPL)](#departments-repl)
    - [List departments](#list-departments)
    - [Show current department](#show-current-department)
    - [Switch department](#switch-department)
    - [My departments](#my-departments)

---

## Installation

### Production hosts (installed via `installer.sh`)

On hosts provisioned by the gSage installer, the CLI and TUI are already
available as system commands — no `pip install`, no venv activation:

```bash
gsage-cli                 # REST CLI / REPL (host wrapper → cli_client.main)
gsage-admin               # Textual admin console (host wrapper → admin_console.main)
gsage-get-admin-key       # reprint / rotate the bootstrap admin API key
```

These wrappers are installed under `/usr/local/bin/` by the installer and use
the operator venv at `/opt/gsage/shared/operator-venv/`. See
[docs-local/architecture/50-INSTALLER.md](../docs-local/architecture/50-INSTALLER.md).

### Development checkout

```bash
# From the project root
pip install -r requirements-cli.txt
```

The REPL entry-point wrapper is `run-cli.sh`.
The batch ingest wrapper is `ingest-documents` (already executable).
The TUI admin console is launched via `run-admin.sh`.

---

## Admin Console (TUI)

A full-featured Terminal User Interface (TUI) for platform administration, built with [Textual](https://textual.textualize.io/).

### Requirements

```bash
pip install -r requirements-cli.txt  # includes textual>=1.0
```

The admin console requires direct access to the services (PostgreSQL, Redis, Elasticsearch, Weaviate, MinIO) — it does **not** go through the backend API.
The `.env` file (or equivalent environment variables) must be present and configured.

### Launching

```bash
# Option 1 — wrapper script
./run-admin.sh

# Option 2 — Python module
python -m admin_console.main

# Option 3 — with custom .env file
./run-admin.sh --env /path/to/.env
```

### Keyboard shortcuts

| Key      | Action                          |
|----------|---------------------------------|
| `F1`     | Show keyboard shortcut help     |
| `F2`     | Switch to Dashboard             |
| `F3`     | Change active organisation      |
| `F5`     | Refresh current panel           |
| `Ctrl+C` | Quit                            |

### Navigation

The sidebar (left pane) is organized into sections that map to panels:

| Section           | Pages                                          |
|-------------------|------------------------------------------------|
| Overview          | Dashboard, Docker status                       |
| Organisations     | Orgs CRUD, Users, Groups, API keys, Approvals  |
| Tools & Interfaces | Tool config, Interface profiles               |
| Data              | Sessions, Datastores, Knowledge base, Files    |
| Jobs              | Scheduled jobs, Background tasks, Email accts  |
| Infrastructure    | Redis inspect, Elasticsearch inspect           |
| Maintenance       | Cache flush, DB cleanup, ES/Weaviate cleanup, Settings |

---

## Authentication

Both tools read credentials from environment variables. Three modes are supported:

### API Key (recommended)

```bash
export GSAGE_API_KEY=gk_live_...          # or gk_test_...
export GSAGE_ORG_ID=<org-uuid>
export GSAGE_API_HOST=http://localhost:8000   # default
```

For terminal-friendly responses (terse, no heavy markdown), create a personal
API key with `interface="cli"`:

```bash
# Create a CLI-optimised personal API key via the HTTP API:
curl -X POST "http://localhost:8000/v1/orgs/$GSAGE_ORG_ID/me/api-keys" \
  -H "Authorization: Bearer $GSAGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-cli-key", "interface": "cli"}'
```

Keys created without an explicit `interface` default to `"web"` (personal keys)
or `"api"` (org-level keys).  The `interface` is always set server-side and
cannot be overridden by a client header.

### Email + Password

```bash
export GSAGE_EMAIL=admin@example.com
export GSAGE_PASSWORD=secret
export GSAGE_API_HOST=http://localhost:8000
```

`GSAGE_ORG_ID` is optional when using email/password — the org is extracted
from the JWT returned by the login call.

### Interactive (REPL only)

Omit all env vars and use the `login` command inside the REPL:

```
> login admin@example.com
Password: ****
✓ Logged in as admin@example.com
Org ID: 2a84139a-8374-48eb-b3ee-2863b0699938
```

---

## Interactive REPL

Start the REPL:

```bash
./run-cli.sh
```

You will see:

```
╔═══════════════════════════════════════════════════════════╗
║  gSage AI — CLI Client                     ║
║  Type 'help' for commands or just chat naturally          ║
╚═══════════════════════════════════════════════════════════╝
>
```

Any text that is **not** a known command is sent as a chat message to the active
conversation. A new conversation is created automatically on the first message.

---

### Authentication commands

| Command | Description |
|---|---|
| `login [email]` | Log in with email + password. If OTP is required, prompts for the code interactively |
| `register` | Create a new account and organization interactively |
| `whoami` | Show current user, org ID, and memberships |

---

### Two-Factor Authentication (OTP) commands

| Command | Description |
|---|---|
| `otp status` | Show 2FA enrollment status (enabled, confirmed date, backup codes remaining) |
| `otp enable` | Interactive TOTP setup: shows secret + provisioning URI, then prompts for confirmation code and displays backup codes |
| `otp disable` | Disable 2FA (prompts for password or current OTP code) |
| `otp backup-codes regenerate` | Generate a new set of 10 backup codes (invalidates current ones) |

**OTP login flow:**  
When the org policy requires OTP, `login` automatically prompts for the code after
password validation.  The user can enter a 6-digit TOTP code **or** a backup code.
After verification, the user is asked whether to remember the device (30-day bypass).

---

### Conversation commands

| Command | Description |
|---|---|
| `conversation list` | List conversations (last 20 active) |
| `conversation new [title]` | Create a new conversation with optional title |
| `conversation show <id>` | Switch to a specific conversation by ID |
| `conversation archive <id>` | Archive (soft-delete) a conversation |
| `messages [limit]` | Show recent messages in the active conversation (default: 10) |

**Example:**

```
> conversation new Security Investigation
✓ Created conversation: Security Investigation

> What is the CVE-2024-12345 vulnerability?
YOU:
What is the CVE-2024-12345 vulnerability?

ASSISTANT:
CVE-2024-12345 is a ...
```

---

### Knowledge base commands

| Command | Description |
|---|---|
| `knowledge search <query>` | Semantic search over the knowledge base |
| `knowledge list [page] [limit]` | List stored documents (default: page 1, limit 20) |
| `knowledge add <name> [--url <url>] [--description <desc>]` | Add a text document (prompts for content via stdin, or fetches from URL) |
| `knowledge delete <id>` | Delete a document by ID (asks for confirmation) |
| `knowledge ingest <file> [--scope org\|user]` | Upload a file for async ingestion |
| `knowledge status <job_id>` | Check the status of an ingest job |

**Examples:**

```
> knowledge search rate limiting strategies
Found 3 result(s):

1. Rate Limiting Best Practices (score: 0.921)
   Token bucket and sliding window algorithms are the two most common ...

> knowledge list
┌──────────────────────────────────────┬──────────────────────────────┬──────┐
│ ID                                   │ Name                         │Status│
├──────────────────────────────────────┼──────────────────────────────┼──────┤
│ 3f4c8b2a-...                         │ Rate Limiting Best Practices │active│
└──────────────────────────────────────┴──────────────────────────────┴──────┘

> knowledge ingest ./docs/runbook.md
✓ Ingest queued: runbook.md
Job ID: 7d46d98c-961b-4030-bfc2-1968c918f2f8
Check status: knowledge status 7d46d98c-961b-4030-bfc2-1968c918f2f8

> knowledge add "FastAPI Docs" --url https://fastapi.tiangolo.com/
URL provided — content will be fetched automatically.
^D
✓ Document added: a1b2c3d4-...

> knowledge add "API Notes" --description "Internal notes on the API design"
Paste content below. Enter a blank line followed by EOF (Ctrl+D) to finish:
The API uses REST with JSON payloads...
^D
✓ Document added: e5f6a7b8-...

> knowledge status 7d46d98c-961b-4030-bfc2-1968c918f2f8
Job ID       7d46d98c-961b-4030-bfc2-1968c918f2f8
Status       COMPLETED
File         runbook.md
Scope        org
Chunks stored  4
```

**Supported file formats for `knowledge ingest`:**

| Format | Extensions |
|---|---|
| PDF | `.pdf` |
| Word | `.docx`, `.doc` |
| Plain text | `.txt`, `.rst` |
| Markdown | `.md` |
| Excel | `.xlsx`, `.xls` |
| PowerPoint | `.pptx`, `.ppt` |
| CSV | `.csv` |
| HTML | `.html`, `.htm` |
| JSON | `.json` |
| XML | `.xml` |
| E-mail | `.eml` |
| ZIP archive | `.zip` |
| TAR archive | `.tar`, `.tar.gz`, `.gz`, `.tar.bz2`, `.tar.xz` |

Maximum size: **10 MB** for documents, **50 MB** for archives.

> When an archive is uploaded, the server extracts it and ingests each supported inner file as a separate set of chunks. A per-file failure inside the archive is non-fatal — the job only fails if no chunks at all could be stored.

**Scope options:**

| Scope | Description |
|---|---|
| `org` | Document is shared across the entire organization (default) |
| `user` | Document is private to the authenticated user |

---

### Approval commands

Human-in-the-loop (HITL) approvals — used when an agent requests permission
to execute a sensitive tool call.

| Command | Description |
|---|---|
| `approvals list [status]` | List approvals (optional filter: `pending`, `approved`, `rejected`) |
| `approvals show <id>` | Show full details of an approval request |
| `approvals approve <id> [comment]` | Approve a pending request (resumes agent run automatically) |
| `approvals reject <id> [comment]` | Reject a pending request |

---

### File commands

Files can be tool-generated (reports, exports, spreadsheets, etc.) or user-uploaded
document templates. Both are stored in separate MinIO buckets and served via the
authenticated backend API proxy.

| Command | Description |
|---|---|
| `files list [page] [limit]` | List files (default: generated, page 1, limit 20) |
| `files list --tool <name>` | Filter generated files by the tool that generated them |
| `files list --all` | Include already-purged files (bytes deleted, record kept) |
| `files list --category generated` | Show only tool-generated files |
| `files list --category template` | Show only document templates |
| `files download <id>` | Download a file to the current directory |
| `files download <id> <dest>` | Download a file to a specific file path |
| `files upload <path>` | Upload a document template |
| `files upload <path> --description "text"` | Upload with a description |
| `files upload <path> --scope org` | Upload as org-wide template (visible to all members) |
| `files delete <id>` | Delete a document template (prompts for confirmation) |

**Notes:**
- Generated files expire after a configurable TTL (default 72 hours). After expiry the
  bytes are deleted by the Celery purge job, but the record remains for audit purposes.
- Templates have no expiry — they persist until explicitly deleted.
- Downloads are streamed directly from the backend API. No presigned URLs or
  external MinIO access is needed.
- Template scope: `user` (default, private) or `org` / `organization` (visible to all
  members of the organisation).
- Allowed template extensions: `.md`, `.docx`, `.xlsx`, `.pptx`, `.pdf`, `.tex`,
  `.zip`, `.txt`, `.csv`, `.json`, `.yaml`, `.yml`, `.html`, `.xml`, `.latex`.

**Example session:**
```
> files list --category template
┌ Files — 2 total (page 1) ────────────────────────────────────────────────────────┐
│ ID           │ Tool        │ Filename            │ Category │ Scope        │ Size  │ Status │
│ 3fa85f64-…   │ user_upload │ incident-report.md  │ template │ organization │ 4 KB  │ never  │
│ ab12cd34-…   │ user_upload │ pentest-template.md │ template │ user         │ 2 KB  │ never  │
└──────────────────────────────────────────────────────────────────────────────────────────┘

> files upload ~/templates/pentest.md --scope org --description "Pentest report base"
✓ Template uploaded: pentest.md (id=cd56ef78…, scope=organization)

> files download 3fa85f64-5717-4562-b3fc-2c963f66afa6 ~/downloads/audit.pdf
✓ Saved to: /home/user/downloads/audit.pdf

> files delete ab12cd34-1234-5678-90ab-cdef01234567
Delete template ab12cd34…? This cannot be undone. [y/N]: y
✓ Template ab12cd34… deleted.
```

---

### Other commands

| Command | Description |
|---|---|
| `editor` | Open `$VISUAL` / `$EDITOR` to compose a multi-line message |
| `debug` | Toggle debug mode (prints HTTP timings, IDs, stack traces) |
| `clear` | Clear the terminal screen |
| `help` | Show all commands |
| `exit` / `quit` | Exit the REPL |

---

### Tab completion

The REPL uses `readline` for Tab completion and arrow-key history navigation.

- **First token**: completes command names (`conversation`, `knowledge`, `approvals`, `files`, …)
- **Second token**: completes subcommands (`list`, `search`, `approve`, …)
- **Third token**: for `approvals show/approve/reject` and `conversation show/archive`,
  fetches IDs from the API and offers them as completions (cached 30 s).

Command history is persisted to `~/.gsage_ai_history` across sessions.

---

## Batch Document Ingest

`ingest-documents` is a standalone script for uploading one or more files to the
knowledge base in bulk, with optional parallelism and status polling.

Supported formats are the same as the REPL `knowledge ingest` command: documents
(pdf, docx, txt, md, html, json, xml, csv, xlsx, pptx, eml …) and archives
(zip, tar, tar.gz, tar.bz2, tar.xz). Archives are extracted server-side; each
inner file becomes its own set of chunks.

### Usage

```
./ingest-documents <FILE_OR_FOLDER> [FILE_OR_FOLDER ...] [OPTIONS]
```

Or equivalently:

```bash
python -m cli_client.ingest <FILE_OR_FOLDER> [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `-r`, `--recursive` | off | Recurse into sub-directories |
| `--scope {org,user}` | `org` | Ingest scope |
| `--parallel N` | `1` | Concurrent uploads (max 8) |
| `--no-wait` | off | Exit immediately after upload; print job IDs |
| `--wait SECONDS` | `120` | Max seconds to wait for all jobs to complete |
| `--dry-run` | off | Discover and print files without uploading |
| `-v`, `--verbose` | off | Enable debug logging |

### Examples

```bash
# Upload a single file
GSAGE_API_KEY=gk_live_... GSAGE_ORG_ID=<uuid> \
  ./ingest-documents report.pdf

# Upload all markdown files in docs/ recursively
GSAGE_API_KEY=gk_live_... GSAGE_ORG_ID=<uuid> \
  ./ingest-documents ./docs/ --recursive

# Upload with user scope, 3 parallel workers, 5-minute timeout
GSAGE_API_KEY=gk_live_... GSAGE_ORG_ID=<uuid> \
  ./ingest-documents ./policies/ -r --scope user --parallel 3 --wait 300

# Dry run — see what would be uploaded
./ingest-documents ./data/ -r --dry-run

# Queue jobs and exit without waiting
GSAGE_API_KEY=gk_live_... GSAGE_ORG_ID=<uuid> \
  ./ingest-documents ./large-archive/ -r --parallel 4 --no-wait
```

### Sample output

```
Found 5 file(s) to ingest.

Uploading 5 file(s) with 2 parallel worker(s)...
  [QUEUED] runbook.md        job_id=7d46d98c-...
  [QUEUED] policy.pdf        job_id=3a8b2c1f-...
  [QUEUED] glossary.txt      job_id=f1e9d7b4-...
  [QUEUED] architecture.docx job_id=2c4f8a09-...
  [QUEUED] metrics.xlsx      job_id=9e1b3d72-...

Queued 5 job(s). Failed to upload: 0.

Waiting up to 120s for ingestion to complete...

==================================================
  Completed : 5
  Failed    : 0
  Timed out : 0
  Upload err: 0
==================================================

  Total chunks stored: 23
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All files uploaded and ingested successfully |
| `1` | Configuration or authentication error (nothing uploaded) |
| `2` | One or more files failed to upload or ingest |

---

## Scheduled Jobs (REPL)

Manage recurring automated tasks backed by RedBeat + Celery.

### List jobs

```
> scheduled list
> scheduled list 1 50 --type PROMPT_RUN --active true
```

### Show job details

```
> scheduled show <job-uuid>
```

### Create a PROMPT_RUN job

```
> scheduled create --name "Daily Security Summary" --cron "0 9 * * 1-5" --prompt "Summarize all security events from the last 24 hours." --tz "America/Sao_Paulo"
```

Cron expression uses standard 5-field syntax: `minute hour day month weekday`.

### Activate / Deactivate

```
> scheduled activate <job-uuid>
> scheduled deactivate <job-uuid>
```

Activation syncs the entry to RedBeat (scheduler); deactivation removes it.

### Delete

```
> scheduled delete <job-uuid>
```

Confirmation is required. Permanently deletes the job and removes it from RedBeat.

---

## Approval Rules (REPL)

Manage tool call approval delegation rules. Each rule specifies which user must approve tool calls matching a combination of org, user, and tool patterns.

### List rules

```
> approval-rules list
> approval-rules list 1 20 --active true
> approval-rules list --tool web_search
```

### Show rule details

```
> approval-rules show <rule-uuid>
```

### Create a rule

```
> approval-rules create --tool web_search --approver <approver-uuid>
> approval-rules create --tool "*" --approver <approver-uuid> --user <user-uuid> --priority 5 --desc "Require approval for all tools for this user"
```

Options:
- `--tool PATTERN` — exact tool name or `*` for all tools (required)
- `--approver USER_ID` — UUID of the user who must approve (required)
- `--user USER_ID|*` — UUID of the user whose calls are matched, or `*` for all users (default: `*`)
- `--priority N` — integer priority, higher wins when multiple rules match (default: 0)
- `--desc "..."` — optional description

### Update a rule

```
> approval-rules update <rule-uuid> --priority 10
> approval-rules update <rule-uuid> --tool file_write --approver <new-approver-uuid>
```

### Activate / Deactivate

```
> approval-rules activate <rule-uuid>
> approval-rules deactivate <rule-uuid>
```

### Delete

```
> approval-rules delete <rule-uuid>
```

Confirmation is required. Permanently deletes the rule.

## DataStores (REPL)

Manage structured data stores and their records. Each store holds JSON records with optional schema validation.

### List stores

```
> datastores list
> datastores list 2 10
```

### Show store details

```
> datastores show <store-uuid>
```

### Create a store

```
> datastores create --name "threat-intel"
> datastores create --name "ioc-feed" --desc "Indicators of compromise" --visibility private --max-records 10000 --schema '{"type":"object"}'
```

Flags:
- `--name` (required) — Store name
- `--desc` — Optional description
- `--visibility` — `shared` (default) or `private`
- `--max-records` — Max number of records (0 = unlimited)
- `--schema` — JSON string describing the expected record structure

### Update a store

```
> datastores update <store-uuid> --name "new-name"
> datastores update <store-uuid> --visibility private --max-records 5000
> datastores update <store-uuid> --activate
> datastores update <store-uuid> --deactivate
```

### Delete a store

```
> datastores delete <store-uuid>
```

Confirmation is required. Permanently deletes the store and **all its records**.

### List records

```
> datastores records <store-uuid>
> datastores records <store-uuid> 2 50
```

### Show a single record

```
> datastores record <store-uuid> <record-uuid>
```

### Add a record

```
> datastores add-record <store-uuid> '{"ip":"10.0.0.1","severity":"high"}'
```

### Update a record

```
> datastores update-record <store-uuid> <record-uuid> '{"severity":"critical"}'
```

### Delete a record

```
> datastores delete-record <store-uuid> <record-uuid>
```

Confirmation is required.

### Query records

```
> datastores query <store-uuid>
> datastores query <store-uuid> '{"severity":"high"}'
```

---

## Departments (REPL)

Manage and switch between departments within the active organization.

> **Note:** DataStore commands require an active department. Set it with `dept set` before using `datastores`.

### List departments

```
> dept list
```

Lists all departments in the active organization (ID, name, slug, default flag, active flag).

### Show current department

```
> dept info
```

Shows the currently active department details.

### Switch department

```
> dept set <dept-id-or-slug>
```

Sets the active department for the current session. All subsequent DataStore commands will target this department.

Can also be set via environment variable:

```bash
export GSAGE_DEPT_ID=<dept-uuid>
```

### My departments

```
> dept my
```

Lists the departments the current user is a member of in the active organization, along with role.

---

## Org Admin (REPL)

Requires the `admin:access` permission. These commands manage organization-level settings, users, groups, tool configurations, interface profiles, and email accounts.

### Organization settings

```
> admin org
> admin org update --name "Acme Corp" --llm-provider openai --llm-api-key sk-...
```

| Flag | Description |
|------|-------------|
| `--name` | Organization display name |
| `--slug` | URL slug |
| `--llm-provider` | LLM provider identifier |
| `--llm-api-key` | LLM API key (encrypted at rest) |
| `--maker-model` | Maker model name |
| `--reviewer-model` | Reviewer model name |
| `--timeout` | Agent timeout in seconds |
| `--max-tokens` | Maximum context tokens |

### Users

```
> admin users list [--search TEXT] [page] [limit]
> admin users show <user_id>
> admin users create --email user@example.com --name "Jane Doe" [--role user|admin]
> admin users reset-password <user_id>   # returns temporary password
> admin users reset-otp <user_id>        # disables OTP for the user
> admin users remove <user_id>           # prompts confirmation
```

### Groups

```
> admin groups list
> admin groups show <group_id>
> admin groups create --name "Security Analysts" [--desc "..."]
> admin groups delete <group_id>
> admin groups permissions              # list all available permissions
```

### Tool Configurations

```
> admin tool-configs list
> admin tool-configs create --tool dns_lookup --profile default --config '{"timeout": 5}' [--desc "..."]
> admin tool-configs delete <config_id>
```

### Interface Profiles

```
> admin interfaces list
> admin interfaces create --interface web [--mode allowlist|denylist] [--tags "dns:read,whois:read"] [--desc "..."]
> admin interfaces delete <profile_id>
```

### Email Accounts

```
> admin emails list
> admin emails create --email inbox@example.com --imap-host imap.example.com --smtp-host smtp.example.com
> admin emails test <account_id>        # tests IMAP and SMTP connectivity
> admin emails delete <account_id>
```

Password fields are prompted interactively and never passed as command-line arguments.
