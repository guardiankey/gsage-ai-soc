#!/bin/bash
# gSage AI — Ollama initialization entrypoint
#
# Starts the Ollama server, pulls required models, and creates a custom
# embedding model with an expanded context window (num_ctx) to prevent
# "input length exceeds context length" errors during document ingestion.
#
# Environment variables (passed from docker-compose):
#   OLLAMA_MAKER_MODEL      LLM maker model to pull on startup (optional — leave empty to skip).
#                           Example: llama3.1:8b. Default: empty (embedding-only mode).
#   OLLAMA_EMBEDDING_MODEL  Custom embedding model name (default: nomic-embed-ctx8k)
#   OLLAMA_EMBED_NUM_CTX    Context window for the embedding model (default: 8192)

set -euo pipefail

MAKER_MODEL="${OLLAMA_MAKER_MODEL:-}"
EMBED_MODEL="${OLLAMA_EMBEDDING_MODEL:-nomic-embed-ctx8k}"
EMBED_NUM_CTX="${OLLAMA_EMBED_NUM_CTX:-8192}"
BASE_EMBED_MODEL="nomic-embed-text"

echo "[ollama-init] Starting Ollama server..."
ollama serve &
OLLAMA_PID=$!

# Wait for the Ollama HTTP API to become available
echo "[ollama-init] Waiting for Ollama API to be ready..."
until ollama list >/dev/null 2>&1; do
    sleep 2
done
echo "[ollama-init] Ollama API is up"

# Pull the base embedding model
echo "[ollama-init] Pulling base embedding model: ${BASE_EMBED_MODEL}"
ollama pull "${BASE_EMBED_MODEL}"

# Create the custom embedding model with the expanded context window.
# Using a distinct name (e.g. nomic-embed-ctx8k) to avoid conflicting
# with future upstream updates to nomic-embed-text.
# NOTE: Ollama >= 0.20 requires -f to point to a file path (stdin pipe no longer works).
echo "[ollama-init] Creating embedding model '${EMBED_MODEL}' with num_ctx=${EMBED_NUM_CTX}"
MODELFILE="/tmp/Modelfile-embed"
printf "FROM %s\nPARAMETER num_ctx %s\n" "${BASE_EMBED_MODEL}" "${EMBED_NUM_CTX}" > "${MODELFILE}"
ollama create "${EMBED_MODEL}" -f "${MODELFILE}"
rm -f "${MODELFILE}"
echo "[ollama-init] Embedding model '${EMBED_MODEL}' is ready"

# Pull the LLM maker model only if OLLAMA_MAKER_MODEL is explicitly set.
# By default this container runs in embedding-only mode (lighter footprint).
if [[ -n "${MAKER_MODEL}" ]]; then
    echo "[ollama-init] Pulling LLM maker model in background: ${MAKER_MODEL}"
    ollama pull "${MAKER_MODEL}" &
else
    echo "[ollama-init] No LLM maker model configured — running in embedding-only mode."
    echo "[ollama-init] Set OLLAMA_MAKER_MODEL (e.g. llama3.1:8b) to enable maker model loading."
fi

echo "[ollama-init] Initialization complete. Server running (PID ${OLLAMA_PID})."
wait "${OLLAMA_PID}"
