#!/usr/bin/env bash
# recreate-weaviate.sh — Destroy and recreate the Weaviate data volume.
#
# WARNING: This permanently deletes ALL vectors and collections stored in
# Weaviate (knowledge base, embeddings, etc.).
# The schema will be recreated automatically on the next application boot.
#
# Usage: ./scripts_operations/recreate-weaviate.sh

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${YELLOW}WARNING: This will permanently destroy the Weaviate data volume.${NC}"
echo -e "${YELLOW}All knowledge base collections and vectors will be lost.${NC}"
read -r -p "Type 'yes' to continue: " confirm
[[ "$confirm" == "yes" ]] || { echo "Aborted."; exit 1; }

echo -e "\n${YELLOW}[1/3] Stopping weaviate container...${NC}"
docker compose down weaviate

echo -e "${YELLOW}[2/3] Removing data volume...${NC}"
docker volume rm gsage-ai_weaviate_data 2>/dev/null \
  && echo "    Volume removed." \
  || echo "    Volume not found — skipping."

echo -e "${YELLOW}[3/3] Starting weaviate container...${NC}"
docker compose up -d weaviate

echo -e "\n${GREEN}Done. Weaviate is starting fresh.${NC}"
echo -e "Collections will be recreated automatically on the next agent request."
echo -e "To reload the default knowledge base run:"
echo -e "  ${YELLOW}celery -A src.backend_api.app.celery_app call src.backend_api.app.tasks.ingest.load_default_knowledge_task --kwargs '{\"org_id\": \"<ORG_UUID>\"}'${NC}"
