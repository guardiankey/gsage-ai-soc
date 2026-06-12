# Curator Admin CLI

A standalone, **stdlib-only** command-line tool (`curator/cli.py`) to administer
Curator reputation lists from inside the Curator container. It calls the Curator
admin HTTP API (`/a/*`), so every write goes through the same validation, upsert
and differential-dump state machine the service uses internally.

It complements the MCP tools `curator_lists` / `curator_manage` by providing a
direct, scriptable interface for operators — including bulk IOC import from a
simple pipe-delimited file.

## Location & invocation

The script ships inside the Curator image at `/app/cli.py` (copied from
`curator/cli.py`). It is executable, so any of these work inside the container:

```bash
docker compose exec curator /app/cli.py collections list
docker compose exec curator python3 /app/cli.py collections list
docker compose exec curator ./cli.py collections list   # cwd is /app
```

## Authentication & connection

| Setting   | Flag         | Environment variable | Default                  |
| --------- | ------------ | -------------------- | ------------------------ |
| API key   | `--api-key`  | `CURATOR_API_KEY`    | — (required)             |
| Base URL  | `--base-url` | `CURATOR_BASE_URL`   | `http://localhost:8000`  |
| Timeout   | `--timeout`  | —                    | `30` seconds             |

The API key is sent as the `X-API-Key` header. Inside the container the default
base URL (`http://localhost:8000`) reaches the local service; from another host
point `--base-url` / `CURATOR_BASE_URL` at the curator service URL.

Add `--json` to any command to emit machine-readable JSON instead of text.

## Commands

### Collections

```bash
# List collections (optionally filter)
cli.py collections list [--active-only] [--published-only]

# Create a collection
cli.py collection create --short-desc "Proxy IPs" --type ip \
    [--subtype tor_exits] [--description "..."] \
    [--active|--no-active] [--published|--no-published]

# Update a collection (by slug or numeric id)
cli.py collection update proxy_ips_ip [--short-desc "..."] [--description "..."] \
    [--active|--no-active] [--published|--no-published]

# Delete a collection and ALL its IOCs (cascade, irreversible)
cli.py collection delete proxy_ips_ip          # prompts for confirmation
cli.py collection delete 7 --yes               # by id, no prompt (for scripts)
```

Collection `--type` must be one of: `ip`, `cidr`, `domain`, `url`,
`domain_regex`, `file_hash_md5`, `file_hash_sha1`, `file_hash_sha256`, `email`,
`asn`, `ja3`, `ja4`.

> **`collection delete` is irreversible.** It hard-deletes the collection row,
> cascade-deletes every IOC it contains (unlike `item del`, which is a
> soft-delete that preserves differential history), and removes the on-disk
> dump directory (`{data_dir}/{slug}/`) so the public `/data/` listing no
> longer serves stale files. Without `--yes` it asks for an interactive
> `yes` confirmation.

### Items (IOCs)

```bash
# Add (upsert) a single IOC — 10-year expiry, internal ticket reference
cli.py item add proxy_lip 10.1.1.1 --type blocklist --expire 10y --ref "ticket #123"

# Delete (soft) a single IOC
cli.py item del proxy_lip 10.1.1.1 --type blocklist

# List / filter IOCs
cli.py item list proxy_lip [--value 10.1.1.1] [--type blocklist] \
    [--page 1] [--per-page 50] [--within-days 30] \
    [--expires-within-days 7] [--never-expires|--no-never-expires] [--expired-only]
```

The collection argument accepts a **slug** (resolved automatically) or a numeric
**collection id**. Block type (`--type`) is one of `blocklist`, `allowlist`,
`suspected`.

#### Expiry format

`--expire` (and the bulk file expiration column) accept a human token:

| Token        | Meaning            |
| ------------ | ------------------ |
| `30d`        | 30 days            |
| `2w`         | 2 weeks (14 days)  |
| `6m`         | 6 months (180 days)|
| `10y`        | 10 years (3650 days)|
| `90`         | 90 days (plain int)|
| empty / `never` / `permanent` | no expiry |

Units use fixed multipliers: `d`=1, `w`=7, `m`=30, `y`=365 days.

### Bulk import

Import many IOCs from a pipe-delimited file **or stdin**:

```bash
# From a file
cli.py bulk import iocs.txt [--dry-run] [--delimiter '|'] [--default-type blocklist]

# From stdin (pipe) — '-' or omitting the path both read stdin
cat /tmp/iocs.csv | docker compose exec -T curator /app/cli.py bulk import -
cat /tmp/iocs.csv | docker compose exec -T curator /app/cli.py bulk import
```

> When piping into `docker exec`, use the `-T` flag (disable TTY allocation) so
> stdin is forwarded to the container.

File format — one IOC per line:

```
slug|block_type|ioc|expiration|reference|public_reference
```

| Column             | Required | Notes                                          |
| ------------------ | :------: | ---------------------------------------------- |
| `slug`             | ✓        | Collection slug or numeric id                  |
| `block_type`       | ✓*       | `blocklist` / `allowlist` / `suspected`        |
| `ioc`              | ✓        | The value (IP, domain, hash, email, ...)       |
| `expiration`       | —        | `10y` / `6m` / `30d` / `N` days / empty = never |
| `reference`        | —        | Internal reference (e.g. `ticket #123`)        |
| `public_reference` | —        | Public source reference (e.g. `CVE-2025-1`)    |

\* `block_type` may be omitted per row when `--default-type` is supplied.

Behaviour:

- Blank lines and lines starting with `#` are ignored.
- Each row is added via the upsert endpoint (re-adding an existing IOC refreshes
  it rather than failing).
- Rows are validated and slugs resolved up-front; invalid rows are **skipped**
  and reported without aborting the batch.
- `--dry-run` validates and resolves everything **without writing**.
- Hard cap of 10,000 rows per invocation.
- Exit code is non-zero if any row failed or was skipped.

Example file:

```
# proxy block list — incident #4711
proxy_lip|blocklist|10.1.1.1|10y|ticket #123
proxy_lip|blocklist|10.1.1.2|6m|ticket #123|CVE-2025-0001
mail_senders|allowlist|good@example.com||trusted partner
```

## Exit codes

| Code | Meaning                                             |
| ---- | --------------------------------------------------- |
| `0`  | Success                                             |
| `1`  | API/validation error, or bulk rows failed/skipped   |
| `2`  | Missing API key                                     |

## Value validation

By default the curator accepts item values **as-is** (only whitespace-trimmed).
This is intentional so reputation lists can hold partial patterns such as URL
paths (`/wp-login.php`) or sender fragments (`@phish.example`).

To enforce strict per-type format checks (reject malformed
domains/urls/emails/hashes/...), set on the curator service:

```
CURATOR_STRICT_VALIDATION=true
```

`ip` and `cidr` collections are **always validated**, regardless of this flag,
because their values are stored in a native PostgreSQL `CIDR` column that
rejects malformed input.
