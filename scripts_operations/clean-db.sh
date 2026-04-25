
#!/usr/bin/env bash
# clean-db.sh — Permanently delete conversations, messages, knowledge base entries
#               and all CGX gSage indices in Elasticsearch.
#
# Usage:
#   ./clean-db.sh               # truncate DB tables + delete ES indices
#   ./clean-db.sh --db-only     # only truncate DB tables
#   ./clean-db.sh --es-only     # only delete ES indices

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────
PG_CONTAINER="gsage-postgres"
PG_USER="gsage"
PG_DB="gsage"
ES_HOST="http://localhost:9200"
ES_INDEX_PATTERN="gsage-*"
TABLES="gsage_agent_runs, gsage_tenant_sessions"

# ── Parse args ────────────────────────────────────────────────────────────
DO_DB=true
DO_ES=true

for arg in "$@"; do
    case "$arg" in
        --db-only) DO_ES=false ;;
        --es-only) DO_DB=false ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

# ── Confirmation ──────────────────────────────────────────────────────────
echo "⚠️  WARNING: This operation is IRREVERSIBLE."
[[ "$DO_DB" == true ]] && echo "   - DB: TRUNCATE $TABLES (CASCADE)"
[[ "$DO_ES" == true ]] && echo "   - ES: DELETE indices matching '$ES_INDEX_PATTERN'"
echo
printf "Type 'yes' to proceed: "
read -r confirmation
if [[ "$confirmation" != "yes" ]]; then
    echo "Aborting."
    exit 0
fi

# ── PostgreSQL ────────────────────────────────────────────────────────────
if [[ "$DO_DB" == true ]]; then
    echo
    echo "[DB] Truncating tables..."
    if ! docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" \
        -c "TRUNCATE $TABLES RESTART IDENTITY CASCADE;"; then
        echo "ERROR: PostgreSQL truncate failed." >&2
        exit 1
    fi
    echo "[DB] Done."
fi

# ── Elasticsearch ─────────────────────────────────────────────────────────
if [[ "$DO_ES" == true ]]; then
    echo
    echo "[ES] Fetching indices matching '$ES_INDEX_PATTERN'..."

    # Use _cat/indices with JSON for reliable parsing
    indices=$(curl -sf "${ES_HOST}/_cat/indices/${ES_INDEX_PATTERN}?h=index" 2>/dev/null || true)

    if [[ -z "$indices" ]]; then
        echo "[ES] No indices found matching '$ES_INDEX_PATTERN'."
    else
        deleted=0
        while IFS= read -r index; do
            [[ -z "$index" ]] && continue
            echo "[ES] Deleting index: $index"
            if curl -sf -X DELETE "${ES_HOST}/${index}" > /dev/null; then
                ((deleted++)) || true
            else
                echo "WARNING: Failed to delete index '$index'" >&2
            fi
        done <<< "$indices"
        echo "[ES] Deleted $deleted index(es)."
    fi
fi

echo
echo "✅ Cleanup complete."
