#!/bin/bash
# Curator database initialisation — runs on first PostgreSQL container start only.
# (Files in /docker-entrypoint-initdb.d/ are executed once when the data volume is empty.)
set -e

CURATOR_DB_USER="${CURATOR_DB_USER:-curator}"
CURATOR_DB_PASSWORD="${CURATOR_DB_PASSWORD:-curator}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${CURATOR_DB_USER}') THEN
            CREATE USER "${CURATOR_DB_USER}" WITH PASSWORD '${CURATOR_DB_PASSWORD}';
        END IF;
    END
    \$\$;

    CREATE DATABASE curator OWNER "${CURATOR_DB_USER}";
    GRANT ALL PRIVILEGES ON DATABASE curator TO "${CURATOR_DB_USER}";
EOSQL
