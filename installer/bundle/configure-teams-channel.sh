#!/usr/bin/env bash
# configure-teams-channel.sh — guided setup for a Microsoft Teams bot channel.
#
# Delegates the DB write to:
#   docker compose exec -T backend_api python -m src.ops_cli channels teams upsert
#
# The Azure App client secret is fed on stdin — never on argv, never logged.
set -euo pipefail

# Resolve the real path so the script works when invoked via a symlink in
# /usr/local/bin/ (e.g. gsage-configure-teams).
_self="${BASH_SOURCE[0]}"
if command -v readlink >/dev/null 2>&1; then
    _self="$(readlink -f "$_self" 2>/dev/null || echo "$_self")"
fi
SCRIPT_DIR="$(cd "$(dirname "$_self")" && pwd)"
GSAGE_HOME="${GSAGE_HOME:-$SCRIPT_DIR}"
COMPOSE_DIR="${GSAGE_COMPOSE_DIR:-$GSAGE_HOME/compose}"
LOG_DIR="${GSAGE_LOG_DIR:-/opt/gsage/shared/logs/helpers}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/configure-teams-channel-$(date -u +%Y%m%dT%H%M%SZ).log"

exec > >(tee -a "$LOG_FILE") 2>&1
echo "── configure-teams-channel $(date -u +%FT%TZ) ──"
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
APP_ID=""
TENANT_ID=""

usage() {
    cat <<'EOF'
Usage: configure-teams-channel.sh [options]
  --org-slug <slug>      Target organization (defaults to single-org install)
  --org-id <uuid>
  --description <str>    Human-readable description shown in admin UI
  --app-id <uuid>        Azure App Registration (client) ID
  --tenant-id <uuid>     Azure tenant ID
  --non-interactive      Fail instead of prompting
  -h, --help
EOF
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --org-slug)      ORG_SLUG="$2"; shift 2 ;;
        --org-id)        ORG_ID="$2"; shift 2 ;;
        --description)   DESCRIPTION="$2"; shift 2 ;;
        --app-id)        APP_ID="$2"; shift 2 ;;
        --tenant-id)     TENANT_ID="$2"; shift 2 ;;
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

Before continuing you need an Azure Bot registration with an App Registration:
  1. In the Azure portal, create an App Registration (or use an existing one).
  2. Under "Certificates & secrets", create a new client secret — copy it now.
  3. Note the Application (client) ID and the Directory (tenant) ID.
  4. Create an Azure Bot resource linked to the App Registration.
  5. After this script completes, set the Bot's messaging endpoint to:
       https://<your-host>/api/v1/channels/teams/<profile_id>/messages

EOM

prompt "Organization slug (blank = single-org install)" ORG_SLUG
prompt "Bot description" DESCRIPTION "Main SOC bot"
prompt "Azure App Registration (client) ID" APP_ID
prompt "Azure tenant ID" TENANT_ID

if [[ $NON_INTERACTIVE -eq 1 ]]; then
    echo "ERROR: --non-interactive cannot prompt for the app client secret" >&2
    exit 2
fi

read -r -s -p "Azure App client secret: " APP_PASSWORD </dev/tty
echo ""
if [[ -z "$APP_PASSWORD" ]]; then
    echo "ERROR: app client secret is required" >&2
    exit 2
fi

args=(
    channels teams upsert
    --description "$DESCRIPTION"
    --app-id "$APP_ID"
    --tenant-id "$TENANT_ID"
    --app-password-stdin
)
[[ -n "$ORG_SLUG" ]] && args+=(--org-slug "$ORG_SLUG")
[[ -n "$ORG_ID"   ]] && args+=(--org-id "$ORG_ID")

echo ""
echo "Running: docker compose exec -T backend_api python -m src.ops_cli ${args[*]}"
echo "(client secret is streamed via stdin — not shown)"

printf '%s\n' "$APP_PASSWORD" | docker compose exec -T backend_api python -m src.ops_cli "${args[@]}"
rc=$?
if [[ $rc -ne 0 ]]; then
    echo ""
    echo "ERROR: ops_cli exited with status $rc" >&2
    exit "$rc"
fi

echo ""
echo "Done. Teams channel active — set the messaging endpoint shown above in your Azure Bot resource."
