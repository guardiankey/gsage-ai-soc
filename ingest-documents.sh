#!/usr/bin/env bash
# ingest-documents — Batch document ingestion tool for gSage AI
#
# Usage: ./ingest-documents <file_or_folder> [options]
# Run ./ingest-documents --help for full usage.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv if present
if [ -d ".venv" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Auto-install CLI dependencies if requirements-cli.txt changed since last install
if [ -f "requirements-cli.txt" ]; then
    if [ ! -f ".cli_deps_installed" ] || [ "requirements-cli.txt" -nt ".cli_deps_installed" ]; then
        pip install -q -r requirements-cli.txt && touch .cli_deps_installed
    fi
fi

exec python -m cli_client.ingest "$@"
