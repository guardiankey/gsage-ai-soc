
# scripts_operations Directory

This directory contains operational, diagnostic, and local-development helper
scripts.

This document is intentionally a stopgap reference:

- It is based only on the text already present inside the scripts in this directory.
- The scripts here are not considered validated.
- Some scripts may be outdated, environment-specific, or destructive.
- Review the script body before executing anything against a real environment.

## Quick Index

| Script | Purpose | Parameters / input |
|---|---|---|
| `bug_async.sh` | Reference-only commands for investigating async issues in the backend container | No CLI parameters |
| `build-dev-image.sh` | Builds the local Docker development image | No CLI parameters |
| `clean-db.sh` | Truncates selected PostgreSQL tables and/or deletes Elasticsearch indices | `--db-only`, `--es-only`, interactive `yes` confirmation |
| `clean-redis-cache.sh` | Flushes selected Redis caches without restarting Redis | `--all`, `--permissions`, `--apikeys`, `--toolcfg` |
| `clean_migrations.sh` | Deletes existing migration files and creates a new baseline migration | No CLI parameters, interactive `yes` confirmation |
| `debug_scheduled_jobs.py` | Inspects the scheduled-job pipeline across PostgreSQL, Redis, Celery, and container logs | `--job-id UUID`, `--tail N`, `--results N` |
| `diagnose_user_ids.sh` | Checks current `user_id` distribution in the Weaviate knowledge collection | No CLI parameters |
| `rebuild-backend.sh` | Rebuilds and recreates the `backend_api` container, then prints recent logs | No CLI parameters |
| `recreate-elasticsearch.sh` | Destroys and recreates the Elasticsearch data volume | No CLI parameters, interactive `yes` confirmation |
| `recreate-minio.sh` | Destroys and recreates the MinIO data volume | No CLI parameters, interactive `yes` confirmation |
| `recreate-postgresql.sh` | Destroys and recreates the PostgreSQL data volume | No CLI parameters, interactive `yes` confirmation |
| `recreate-redis.sh` | Destroys and recreates the Redis data volume | No CLI parameters, interactive `yes` confirmation |
| `recreate-weaviate.sh` | Destroys and recreates the Weaviate data volume | No CLI parameters, interactive `yes` confirmation |
| `test_elasticsearch.py` | Tests Elasticsearch connectivity and attempts to initialize templates and ILM policies | No CLI parameters |
| `test_nvd_lookup.py` | Runs a multi-step NVD API / `nvdlib` diagnostic workflow | No CLI parameters; optionally uses `TOOL_NVD_LOOKUP__API_KEY` |
| `test_setup.py` | Verifies that core imports and Elasticsearch definitions load correctly | No CLI parameters |
| `weaviate_data.sh` | Lists Weaviate collections and shows sample objects | Optional positional limit: `N` |

## General Reading Notes

- Shell scripts usually expect to be run from the repository root.
- Several scripts call Docker directly and assume the local container names and services embedded in the script are correct.
- Destructive scripts usually require typing `yes` interactively.
- Some scripts contain hardcoded container names, passwords, or service names. Treat them as local helpers, not production-grade tooling.

## Detailed Parameter Reference

### `bug_async.sh`

Reference file with commented commands for investigating async issues, high CPU,
hanging requests, and document-ingestion problems.

#### Usage

```bash
bash scripts_operations/bug_async.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The current file exits immediately with `exit 0`.
- The remaining content is a set of commented or manual `py-spy`, `docker`, and process-inspection commands.

### `build-dev-image.sh`

Builds the Docker image `gsage-python-dev-image` using the `dev` target from
`docker/Dockerfile`.

#### Usage

```bash
bash scripts_operations/build-dev-image.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script resolves the project root automatically.
- The image name is hardcoded as `gsage-python-dev-image`.

### `clean-db.sh`

Deletes selected database records and/or Elasticsearch indices.

#### Usage

```bash
./scripts_operations/clean-db.sh
./scripts_operations/clean-db.sh --db-only
./scripts_operations/clean-db.sh --es-only
```

#### Parameters

| Parameter | Required | Description |
|---|---|---|
| `--db-only` | No | Only truncates the configured PostgreSQL tables |
| `--es-only` | No | Only deletes Elasticsearch indices matching the configured pattern |

#### Notes

- With no flags, the script does both operations.
- The script asks for an interactive `yes` confirmation before continuing.
- The current hardcoded PostgreSQL tables are `gsage_agent_runs` and `gsage_tenant_sessions`.
- The current Elasticsearch index pattern is `gsage-*`.

### `clean-redis-cache.sh`

Flushes selected Redis cache keys without restarting the Redis container.

#### Usage

```bash
./scripts_operations/clean-redis-cache.sh
./scripts_operations/clean-redis-cache.sh --all
./scripts_operations/clean-redis-cache.sh --permissions
./scripts_operations/clean-redis-cache.sh --apikeys
./scripts_operations/clean-redis-cache.sh --toolcfg
```

#### Parameters

| Parameter | Required | Description |
|---|---|---|
| none | No | Safe default: flushes permission caches, API key caches, and tool config caches |
| `--all` | No | Flushes the entire Redis DB |
| `--permissions` | No | Flushes only permission caches |
| `--apikeys` | No | Flushes API key caches and revoked API key caches |
| `--toolcfg` | No | Flushes tool configuration caches |

#### Notes

- The script reads `REDIS_PASSWORD` from `.env`.
- The current Redis container name is hardcoded as `gsage-redis`.
- The header comment explicitly says it does not touch circuit breaker state, rate limit counters, or scheduled locks, although `--all` flushes the full DB.

### `clean_migrations.sh`

Deletes migration files and creates a new autogenerated baseline migration.

#### Usage

```bash
bash scripts_operations/clean_migrations.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script asks for an interactive `yes` confirmation.
- It removes `src/migrations/versions/*.py` and `*.pyc`.
- It then runs `alembic revision --autogenerate -m "initial_schema"` and `alembic upgrade head`.
- Treat this as a destructive local reset helper.

### `debug_scheduled_jobs.py`

Runs a scheduled-job diagnostic workflow across the database, RedBeat keys,
Celery task results, and container logs.

#### Usage

```bash
python scripts_operations/debug_scheduled_jobs.py
python scripts_operations/debug_scheduled_jobs.py --job-id <uuid>
python scripts_operations/debug_scheduled_jobs.py --tail 100
python scripts_operations/debug_scheduled_jobs.py --results 10
```

#### Parameters

| Parameter | Required | Description |
|---|---|---|
| `--job-id UUID` | No | Filters the output to a specific scheduled job ID |
| `--tail N` | No | Number of log lines to inspect per container. Default: `100` |
| `--results N` | No | Number of Celery task results to display. Default: `10` |

#### Notes

- The current script uses hardcoded container names such as `gsage-redis`, `gsage-postgres`, `gsage-celery-beat`, and `gsage-celery-scheduled`.
- The Redis password is currently hardcoded in the script as `dev-redis-password`.

### `diagnose_user_ids.sh`

Checks the distribution of `org_id:user_id` values in the shared Weaviate
knowledge collection.

#### Usage

```bash
bash scripts_operations/diagnose_user_ids.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The current command uses `docker compose exec backend python -c ...`.
- It fetches up to 100 objects and prints a count grouped by `org_id:user_id`.

### `rebuild-backend.sh`

Quick rebuild helper for backend development.

#### Usage

```bash
bash scripts_operations/rebuild-backend.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- It runs `docker compose build backend_api`.
- It then runs `docker compose up -d --force-recreate backend_api`.
- Finally, it prints the last 50 lines from `docker compose logs backend_api`.

### `recreate-elasticsearch.sh`

Destroys and recreates the Elasticsearch data volume.

#### Usage

```bash
./scripts_operations/recreate-elasticsearch.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script requires typing `yes` to continue.
- It removes the Docker volume `gsage-ai_elasticsearch_data`.
- The script text says schema and collections will be recreated automatically on the next boot / request.
- It also prints a suggested Celery command for reloading the default knowledge base.

### `recreate-minio.sh`

Destroys and recreates the MinIO data volume.

#### Usage

```bash
./scripts_operations/recreate-minio.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script requires typing `yes` to continue.
- It removes the Docker volume `gsage-ai_minio_data`.
- The header warns that MinIO objects such as attachments, tool artifacts, and reports will be lost.

### `recreate-postgresql.sh`

Destroys and recreates the PostgreSQL data volume.

#### Usage

```bash
./scripts_operations/recreate-postgresql.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script requires typing `yes` to continue.
- It removes the Docker volume `gsage-ai_postgres_data`.
- The script text says to run `alembic upgrade head` afterwards.

### `recreate-redis.sh`

Destroys and recreates the Redis data volume.

#### Usage

```bash
./scripts_operations/recreate-redis.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script requires typing `yes` to continue.
- It removes the Docker volume `gsage-ai_redis_data`.
- The header warns that caches, Celery queues, rate-limit counters, and session data will be lost.

### `recreate-weaviate.sh`

Destroys and recreates the Weaviate data volume.

#### Usage

```bash
./scripts_operations/recreate-weaviate.sh
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script requires typing `yes` to continue.
- It removes the Docker volume `gsage-ai_weaviate_data`.
- The script text says collections will be recreated automatically and prints a suggested Celery command for reloading the default knowledge base.

### `test_elasticsearch.py`

Attempts to connect to Elasticsearch, prints cluster information, and then tries
to create ILM policies and index templates.

#### Usage

```bash
python scripts_operations/test_elasticsearch.py
```

#### Parameters

This script has no CLI parameters.

#### Notes

- The script imports application settings to resolve the Elasticsearch URL.
- It is a diagnostic / initialization helper, not a formal test suite.

### `test_nvd_lookup.py`

Runs a multi-step diagnostic against the NVD API and `nvdlib` behavior.

#### Usage

```bash
python scripts_operations/test_nvd_lookup.py
docker compose exec mcp-server python scripts_operations/test_nvd_lookup.py
```

#### Parameters

This script has no CLI parameters.

#### Environment input

| Variable | Required | Description |
|---|---|---|
| `TOOL_NVD_LOOKUP__API_KEY` | No | Optional NVD API key used in tests that support authenticated requests |

#### Notes

- The script runs several independent checks, including DNS resolution, raw `httpx` calls, and `nvdlib.searchCVE(...)` calls.
- Failures in one check do not stop the others.

### `test_setup.py`

Quick import and settings sanity check.

#### Usage

```bash
python scripts_operations/test_setup.py
```

#### Parameters

This script has no CLI parameters.

#### Notes

- It verifies that settings and Elasticsearch definitions can be imported.
- On success, it prints a hint to run `python scripts/init-elasticsearch.py`.

### `weaviate_data.sh`

Prints the current Weaviate collections and shows a sample of objects from the
shared collection and any `kb_*` collections.

#### Usage

```bash
./scripts_operations/weaviate_data.sh
./scripts_operations/weaviate_data.sh 20
```

#### Parameters

| Parameter | Required | Description |
|---|---|---|
| `N` | No | Positional limit for how many objects to show per collection. Default: `5` |

#### Notes

- The script executes Python inside `docker compose exec backend_api`.
- The parameter is interpreted as an integer inside the embedded Python code.

