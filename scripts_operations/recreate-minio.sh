#!/usr/bin/env bash
# recreate-minio.sh — Destroy and recreate the MinIO data volume.
#
# WARNING: This permanently deletes ALL objects stored in MinIO
# (tool artifacts, file attachments, generated reports, etc.).
#
# Usage: ./scripts_operations/recreate-minio.sh

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${YELLOW}WARNING: This will permanently destroy the MinIO data volume.${NC}"
echo -e "${RED}All stored objects (attachments, tool artifacts, reports) will be lost.${NC}"
read -r -p "Type 'yes' to continue: " confirm
[[ "$confirm" == "yes" ]] || { echo "Aborted."; exit 1; }

echo -e "\n${YELLOW}[1/3] Stopping minio container...${NC}"
docker compose down minio

echo -e "${YELLOW}[2/3] Removing data volume...${NC}"
docker volume rm gsage-ai_minio_data 2>/dev/null \
  && echo "    Volume removed." \
  || echo "    Volume not found — skipping."

echo -e "${YELLOW}[3/3] Starting minio container...${NC}"
docker compose up -d minio

echo -e "\n${GREEN}Done. MinIO is starting fresh.${NC}"
echo -e "Buckets will be recreated automatically on next backend startup."
