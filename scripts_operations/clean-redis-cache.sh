#!/usr/bin/env bash
# clean-redis-cache.sh — Flush gSageAI Redis caches without restarting containers.
#
# Usage:
#   ./clean-redis-cache.sh              # flush permission + API key caches (safe default)
#   ./clean-redis-cache.sh --all        # flush EVERYTHING in Redis (use with care)
#   ./clean-redis-cache.sh --permissions  # only permission caches
#   ./clean-redis-cache.sh --apikeys      # only API key caches
#
# Does NOT touch: circuit breaker states, rate limit counters, scheduled locks.


set -euo pipefail

# ── Load Redis password from .env ─────────────────────────────────────────
ENV_FILE="$(dirname "$0")/../.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env file not found at $ENV_FILE" >&2
    exit 1
fi

REDIS_PASS="$(grep -E '^REDIS_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")"
if [[ -z "$REDIS_PASS" ]]; then
    echo "ERROR: REDIS_PASSWORD not set in .env" >&2
    exit 1
fi

redis_cli() {
    docker exec gsage-redis redis-cli -a "$REDIS_PASS" "$@" 2>/dev/null
}

# ── Helpers ───────────────────────────────────────────────────────────────

flush_pattern() {
    local pattern="$1"
    local label="$2"
    local keys
    # SCAN instead of KEYS to avoid blocking on large datasets
    mapfile -t keys < <(redis_cli --no-auth-warning SCAN 0 MATCH "$pattern" COUNT 1000 | tail -n +2)
    if [[ ${#keys[@]} -eq 0 ]]; then
        echo "  [$label] No keys matched '$pattern'"
        return
    fi
    local count=0
    for key in "${keys[@]}"; do
        [[ -z "$key" ]] && continue
        redis_cli --no-auth-warning DEL "$key" > /dev/null
        ((count++)) || true
    done
    echo "  [$label] Deleted $count key(s) matching '$pattern'"
}

flush_permissions() {
    echo "Flushing permission caches..."
    flush_pattern "cache:permissions:*" "permissions"
}

flush_apikeys() {
    echo "Flushing API key caches..."
    flush_pattern "cache:apikey:*" "apikey"
    flush_pattern "revoked:apikey:*" "revoked-apikey"
}

flush_toolcfg() {
    echo "Flushing tool config caches..."
    flush_pattern "toolcfg:*" "toolcfg"
}

flush_all() {
    echo "WARNING: Flushing ALL Redis keys..."
    redis_cli --no-auth-warning FLUSHDB
    echo "  [all] Redis DB flushed"
}

# ── Main ──────────────────────────────────────────────────────────────────

MODE="${1:-}"

echo "=== gSageAI Redis Cache Cleaner ==="
echo "Container: gsage-redis"
echo ""

case "$MODE" in
    --all)
        flush_all
        ;;
    --permissions)
        flush_permissions
        ;;
    --apikeys)
        flush_apikeys
        ;;
    --toolcfg)
        flush_toolcfg
        ;;
    "")
        # Safe default: flush auth caches + tool config caches
        flush_permissions
        flush_apikeys
        flush_toolcfg
        ;;
    *)
        echo "Unknown option: $MODE"
        echo "Usage: $0 [--all | --permissions | --apikeys | --toolcfg]"
        exit 1
        ;;
esac

echo ""
echo "Done. Remaining keys in Redis:"
redis_cli --no-auth-warning KEYS "*" | sort
