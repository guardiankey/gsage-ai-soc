#!/usr/bin/env bash
# configure-telegram-channel.sh — guided setup for a Telegram bot channel.
#
# Delegates the DB write to:
#   docker compose exec -T backend_api python -m src.ops_cli channels telegram upsert
#
# The bot token is fed on stdin — never on argv, never logged.
set -euo pipefail

# Resolve the real path so the script works when invoked via a symlink in
# /usr/local/bin/ (e.g. gsage-configure-telegram).
_self="${BASH_SOURCE[0]}"
if command -v readlink >/dev/null 2>&1; then
    _self="$(readlink -f "$_self" 2>/dev/null || echo "$_self")"
fi
SCRIPT_DIR="$(cd "$(dirname "$_self")" && pwd)"
GSAGE_HOME="${GSAGE_HOME:-$SCRIPT_DIR}"
COMPOSE_DIR="${GSAGE_COMPOSE_DIR:-$GSAGE_HOME/compose}"
LOG_DIR="${GSAGE_LOG_DIR:-/opt/gsage/shared/logs/helpers}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/configure-telegram-channel-$(date -u +%Y%m%dT%H%M%SZ).log"

exec > >(tee -a "$LOG_FILE") 2>&1
echo "── configure-telegram-channel $(date -u +%FT%TZ) ──"
echo "   log: $LOG_FILE"
if [[ ! -f "$COMPOSE_DIR/docker-compose.yml" ]]; then
    echo "ERROR: docker-compose.yml not found at $COMPOSE_DIR" >&2
    exit 1
fi
cd "$COMPOSE_DIR"

NON_INTERACTIVE=0
ORG_SLUG=""
ORG_ID=""
DESCRIPTION=""

usage() {
    cat <<'EOF'
Usage: configure-telegram-channel.sh [options]
  --org-slug <slug>      Target organization (defaults to single-org install)
  --org-id <uuid>
  --description <str>    Human-readable description shown in admin UI
  --non-interactive      Fail instead of prompting
  -h, --help
EOF
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --org-slug) ORG_SLUG="$2"; shift 2 ;;
        --org-id)   ORG_ID="$2"; shift 2 ;;
        --description) DESCRIPTION="$2"; shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

prompt() {
    local label="$1" var="$2" default="${3:-}" cur
    cur="${!var}"
    if [[ -n "$cur" ]]; then return 0; fi
    if [[ $NON_INTERACTIVE -eq 1 ]]; then
        echo "ERROR: --non-interactive but missing $var" >&2
        exit 2
    fi
    if [[ -n "$default" ]]; then
        read -r -p "$label [$default]: " ans
        ans="${ans:-$default}"
    else
        read -r -p "$label: " ans
    fi
    printf -v "$var" '%s' "$ans"
}

cat <<'EOM'

Before continuing you need a bot token issued by @BotFather on Telegram:
  1. Open a chat with @BotFather.
  2. Run /newbot and pick a name + username.
  3. Copy the token shown by BotFather.

EOM

prompt "Organization slug (blank = single-org install)" ORG_SLUG
prompt "Bot description" DESCRIPTION "Main SOC bot"

if [[ $NON_INTERACTIVE -eq 1 ]]; then
    echo "ERROR: --non-interactive cannot prompt for the bot token" >&2
    exit 2
fi

read -r -s -p "Telegram bot token: " BOT_TOKEN </dev/tty
echo ""
if [[ -z "$BOT_TOKEN" ]]; then
    echo "ERROR: bot token is required" >&2
    exit 2
fi

args=(
    channels telegram upsert
    --description "$DESCRIPTION"
    --bot-token-stdin
)
[[ -n "$ORG_SLUG" ]] && args+=(--org-slug "$ORG_SLUG")
[[ -n "$ORG_ID"   ]] && args+=(--org-id "$ORG_ID")

echo ""
echo "Running: docker compose exec -T backend_api python -m src.ops_cli ${args[*]}"
echo "(token is streamed via stdin — not shown)"

printf '%s\n' "$BOT_TOKEN" | docker compose exec -T backend_api python -m src.ops_cli "${args[@]}"
rc=$?
if [[ $rc -ne 0 ]]; then
    echo ""
    echo "ERROR: ops_cli exited with status $rc" >&2
    exit "$rc"
fi

echo ""
echo "Done. Telegram channel active — the telegram_worker will pick it up within TELEGRAM_RELOAD_INTERVAL seconds."
