#!/usr/bin/env bash
# run-admin.sh — launch the gSage TUI Admin Console
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -d ".venv" ]]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
fi

export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"

exec python -m admin_console.main "$@"
