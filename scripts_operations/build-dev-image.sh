#!/usr/bin/env bash
# Build the dev base image used by docker-compose for local development.
# Run from the project root:
#   bash scripts_operations/build-dev-image.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_NAME="gsage-python-dev-image"

echo "══════════════════════════════════════════════════════════"
echo "  Building dev image: $IMAGE_NAME (target=dev)"
echo "══════════════════════════════════════════════════════════"

docker build \
  -t "$IMAGE_NAME" \
  -f "$PROJECT_ROOT/docker/Dockerfile" \
  --target dev \
  "$PROJECT_ROOT"

echo ""
echo "  Imagem criada: $IMAGE_NAME"
echo "  Use: docker compose up -d"
echo "══════════════════════════════════════════════════════════"
