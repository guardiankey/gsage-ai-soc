#!/usr/bin/env bash
# recreate-postgresql.sh — Destroy and recreate the PostgreSQL data volume.
#
# WARNING: This permanently deletes ALL data stored in PostgreSQL.
# Run alembic upgrade head afterwards to recreate the schema.
#
# Usage: ./scripts_operations/recreate-postgresql.sh

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${YELLOW}WARNING: This will permanently destroy the PostgreSQL data volume.${NC}"
read -r -p "Type 'yes' to continue: " confirm
[[ "$confirm" == "yes" ]] || { echo "Aborted."; exit 1; }

echo -e "\n${YELLOW}[1/3] Stopping postgres container...${NC}"
docker compose down postgres

echo -e "${YELLOW}[2/3] Removing data volume...${NC}"
docker volume rm gsage-ai-soc_postgres_data 2>/dev/null \
  && echo "    Volume removed." \
  || echo "    Volume not found — skipping."

echo -e "${YELLOW}[3/3] Starting postgres container...${NC}"
docker compose up -d postgres

echo -e "\n${GREEN}Done. PostgreSQL is starting fresh.${NC}"
echo -e "Run ${YELLOW}alembic upgrade head${NC} to recreate the schema."
