#!/bin/bash
# Wiki.js database initialisation — runs on first PostgreSQL container start only.
# (Files in /docker-entrypoint-initdb.d/ are executed once when the data volume is empty.)
set -e

WIKIJS_DB_USER="${WIKIJS_DB_USER:-wikijs}"
WIKIJS_DB_PASSWORD="${WIKIJS_DB_PASSWORD:-wikijs}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${WIKIJS_DB_USER}') THEN
            CREATE USER "${WIKIJS_DB_USER}" WITH PASSWORD '${WIKIJS_DB_PASSWORD}';
        END IF;
    END
    \$\$;

    CREATE DATABASE wikijs OWNER "${WIKIJS_DB_USER}";
    GRANT ALL PRIVILEGES ON DATABASE wikijs TO "${WIKIJS_DB_USER}";
EOSQL
