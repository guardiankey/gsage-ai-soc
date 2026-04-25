#!/usr/bin/env bash
# recreate-redis.sh — Destroy and recreate the Redis data volume.
#
# WARNING: This permanently deletes ALL data stored in Redis (cache,
# Celery queues, rate-limit counters, session data, etc.).
# Redis will start empty after this operation.
#
# Usage: ./scripts_operations/recreate-redis.sh

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${YELLOW}WARNING: This will permanently destroy the Redis data volume.${NC}"
echo -e "${YELLOW}All cache, queues, and session data will be lost.${NC}"
read -r -p "Type 'yes' to continue: " confirm
[[ "$confirm" == "yes" ]] || { echo "Aborted."; exit 1; }

echo -e "\n${YELLOW}[1/3] Stopping redis container...${NC}"
docker compose down redis

echo -e "${YELLOW}[2/3] Removing data volume...${NC}"
docker volume rm gsage-ai_redis_data 2>/dev/null \
  && echo "    Volume removed." \
  || echo "    Volume not found — skipping."

echo -e "${YELLOW}[3/3] Starting redis container...${NC}"
docker compose up -d redis

echo -e "\n${GREEN}Done. Redis is starting fresh.${NC}"
