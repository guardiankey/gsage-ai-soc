#!/usr/bin/env bash
# configure-email-channel.sh — guided setup for an IMAP/SMTP mailbox.
#
# Runs inside /opt/gsage/current/ and delegates the actual DB write to:
#   docker compose exec -T backend_api python -m ops_cli channels email create
#
# Secrets (IMAP/SMTP passwords) are fed on stdin — never on argv, never logged.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GSAGE_HOME="${GSAGE_HOME:-$SCRIPT_DIR}"
LOG_DIR="${GSAGE_LOG_DIR:-/opt/gsage/shared/logs/helpers}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/configure-email-channel-$(date -u +%Y%m%dT%H%M%SZ).log"

# Log everything except stdin (so secrets don't reach the log).
exec > >(tee -a "$LOG_FILE") 2>&1
echo "── configure-email-channel $(date -u +%FT%TZ) ──"
echo "   log: $LOG_FILE"
cd "$GSAGE_HOME"

NON_INTERACTIVE=0
ORG_SLUG=""
ORG_ID=""
TEST_PROBE=0
DISPLAY_NAME=""
EMAIL_ADDR=""
IMAP_HOST=""; IMAP_PORT="993"; IMAP_USER=""
SMTP_HOST=""; SMTP_PORT="587"; SMTP_USER=""

usage() {
    cat <<'EOF'
Usage: configure-email-channel.sh [options]
  --org-slug <slug>        Target organization (defaults to single-org install)
  --org-id <uuid>
  --display-name <str>
  --email <addr>
  --imap-host <host> --imap-port <int> --imap-user <user>
  --smtp-host <host> --smtp-port <int> --smtp-user <user>
  --test                   Probe IMAP LOGIN + SMTP AUTH before saving
  --non-interactive        Fail instead of prompting for missing fields
  -h, --help
EOF
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --org-slug) ORG_SLUG="$2"; shift 2 ;;
        --org-id)   ORG_ID="$2"; shift 2 ;;
        --display-name) DISPLAY_NAME="$2"; shift 2 ;;
        --email) EMAIL_ADDR="$2"; shift 2 ;;
        --imap-host) IMAP_HOST="$2"; shift 2 ;;
        --imap-port) IMAP_PORT="$2"; shift 2 ;;
        --imap-user) IMAP_USER="$2"; shift 2 ;;
        --smtp-host) SMTP_HOST="$2"; shift 2 ;;
        --smtp-port) SMTP_PORT="$2"; shift 2 ;;
        --smtp-user) SMTP_USER="$2"; shift 2 ;;
        --test) TEST_PROBE=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

prompt() {
    # $1 = label, $2 = var name, $3 = default (optional)
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

prompt_secret() {
    # $1 = label, $2 = var name (exported via global, not echoed to log)
    local label="$1" var="$2" ans
    if [[ $NON_INTERACTIVE -eq 1 ]]; then
        echo "ERROR: --non-interactive cannot prompt for $var" >&2
        exit 2
    fi
    read -r -s -p "$label: " ans </dev/tty
    echo ""  # newline after hidden read
    printf -v "$var" '%s' "$ans"
}

# ── Collect non-secret fields ──────────────────────────────────────────
prompt "Organization slug (blank = single-org install)" ORG_SLUG
prompt "Display name" DISPLAY_NAME "SOC Mailbox"
prompt "Email address" EMAIL_ADDR
prompt "IMAP host" IMAP_HOST
prompt "IMAP port" IMAP_PORT "993"
prompt "IMAP username" IMAP_USER "$EMAIL_ADDR"
prompt "SMTP host" SMTP_HOST "$IMAP_HOST"
prompt "SMTP port" SMTP_PORT "587"
prompt "SMTP username" SMTP_USER "$IMAP_USER"

# ── Secrets via stdin (never logged) ──────────────────────────────────
IMAP_PW=""
SMTP_PW=""
prompt_secret "IMAP password" IMAP_PW
prompt_secret "SMTP password (leave empty for unauthenticated relay)" SMTP_PW

# ── Build args and exec ────────────────────────────────────────────────
args=(
    channels email create
    --display-name "$DISPLAY_NAME"
    --email "$EMAIL_ADDR"
    --imap-host "$IMAP_HOST" --imap-port "$IMAP_PORT" --imap-user "$IMAP_USER"
    --smtp-host "$SMTP_HOST" --smtp-port "$SMTP_PORT" --smtp-user "$SMTP_USER"
    --imap-password-stdin
)
[[ -n "$SMTP_PW" ]] && args+=(--smtp-password-stdin)
[[ -n "$ORG_SLUG" ]] && args+=(--org-slug "$ORG_SLUG")
[[ -n "$ORG_ID"   ]] && args+=(--org-id "$ORG_ID")
[[ $TEST_PROBE -eq 1 ]] && args+=(--test)

echo ""
echo "Running: docker compose exec -T backend_api python -m ops_cli ${args[*]}"
echo "(passwords are streamed via stdin — not shown)"

# Print secrets separated by a newline; ops_cli reads two lines when both
# --imap-password-stdin and --smtp-password-stdin are set.
if [[ -n "$SMTP_PW" ]]; then
    printf '%s\n%s\n' "$IMAP_PW" "$SMTP_PW"
else
    printf '%s\n' "$IMAP_PW"
fi | docker compose exec -T backend_api python -m ops_cli "${args[@]}"

rc=$?
if [[ $rc -ne 0 ]]; then
    echo ""
    echo "ERROR: ops_cli exited with status $rc" >&2
    exit "$rc"
fi
echo ""
echo "Done. Email account stored. Worker will start polling within the next cycle."
