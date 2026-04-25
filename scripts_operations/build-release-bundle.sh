#!/usr/bin/env bash
# build-release-bundle.sh — assemble a self-contained gSage release bundle.
#
# Consumes images already published by publish-images.sh at
# $REGISTRY/gsage-<name>:<tag> and packages a tarball that ships:
#   - installer.sh (+ wizard + preflight libs)
#   - docker-compose.prod.yml
#   - env-installer.template
#   - configure-*-channel.sh helpers
#   - ops_cli + cli_client + admin_console + src/shared
#   - bin/gsage-* host wrappers
#   - requirements-operator.txt
#   - docker/{postgres/init-curator.sh,ollama/entrypoint.sh}
#   - dbs/*/update.sh (GeoIP refreshers — keep as-is, no binary DBs)
#   - scripts/get_admin.py
#   - MANIFEST.json with exact image digests pulled from the registry
#
# Usage:
#   scripts_operations/build-release-bundle.sh \
#       --version 0.1.0 \
#       --registry guardiankey \
#       [--output-dir dist/] [--dry-run]
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VERSION=""
REGISTRY=""
OUTPUT_DIR="$PROJECT_ROOT/dist"
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: build-release-bundle.sh --version <ver> --registry <ns> [options]

Required:
  --version <ver>        Release version (e.g. 0.1.0). Defaults to ./VERSION.
  --registry <namespace> Docker Hub / registry namespace (e.g. guardiankey).

Optional:
  --output-dir <dir>     Where to write the tarball. Defaults to ./dist.
  --dry-run              Stage the bundle but do not tar or compute digests.
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --registry) REGISTRY="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    if [[ -f "$PROJECT_ROOT/VERSION" ]]; then
        VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/VERSION")"
    else
        echo "ERROR: --version not given and ./VERSION not found" >&2
        exit 1
    fi
fi
if [[ -z "$REGISTRY" ]]; then
    echo "ERROR: --registry is required" >&2
    exit 1
fi

# ── Validate tools ────────────────────────────────────────────────────
for tool in docker tar sha256sum jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: required tool '$tool' not found in PATH" >&2
        exit 1
    fi
done

BUNDLE_NAME="gsage-${VERSION}"
STAGING="$OUTPUT_DIR/staging/$BUNDLE_NAME"
TARBALL="$OUTPUT_DIR/${BUNDLE_NAME}.tar.gz"

echo "== build-release-bundle =="
echo "   version : $VERSION"
echo "   registry: $REGISTRY"
echo "   output  : $OUTPUT_DIR"
echo "   staging : $STAGING"
echo ""

rm -rf "$STAGING"
mkdir -p "$STAGING" "$OUTPUT_DIR"

# ── Validate that every required image exists in the registry ─────────
IMAGES=(backend_api worker_tools mcp_server frontend curator)
declare -A IMAGE_DIGESTS
for name in "${IMAGES[@]}"; do
    ref="${REGISTRY}/gsage-${name}:${VERSION}"
    echo ">> verifying $ref"
    if ! digest_json="$(docker manifest inspect "$ref" 2>/dev/null)"; then
        echo "ERROR: image $ref not found in registry. Run publish-images.sh first." >&2
        exit 2
    fi
    # Multi-arch manifest list or single manifest — capture the config digest.
    digest="$(echo "$digest_json" | jq -r '
        if .manifests then .manifests[0].digest
        elif .config.digest then .config.digest
        else "unknown" end
    ')"
    IMAGE_DIGESTS[$name]="$digest"
    echo "   digest: $digest"
done

# ── Stage files ───────────────────────────────────────────────────────
echo ""
echo ">> staging files"

cp "$PROJECT_ROOT/VERSION" "$STAGING/VERSION"

# Installer + libs + compose + env template + helpers
mkdir -p "$STAGING/compose" "$STAGING/bin"
cp "$PROJECT_ROOT/installer/installer.sh"                  "$STAGING/installer.sh"
cp "$PROJECT_ROOT/installer/wizard.lib.sh"                 "$STAGING/wizard.lib.sh"
cp "$PROJECT_ROOT/installer/preflight.lib.sh"              "$STAGING/preflight.lib.sh"
cp "$PROJECT_ROOT/installer/compose/docker-compose.prod.yml" "$STAGING/compose/docker-compose.yml"
cp "$PROJECT_ROOT/installer/env-installer.template"        "$STAGING/env.template"
cp "$PROJECT_ROOT/installer/bundle/configure-email-channel.sh"    "$STAGING/configure-email-channel.sh"
cp "$PROJECT_ROOT/installer/bundle/configure-telegram-channel.sh" "$STAGING/configure-telegram-channel.sh"
cp "$PROJECT_ROOT/installer/bundle/bin/gsage-cli"          "$STAGING/bin/gsage-cli"
cp "$PROJECT_ROOT/installer/bundle/bin/gsage-admin"        "$STAGING/bin/gsage-admin"
cp "$PROJECT_ROOT/installer/bundle/bin/gsage-get-admin-key" "$STAGING/bin/gsage-get-admin-key"

# Python source (only what the host wrappers / operator venv need)
cp "$PROJECT_ROOT/requirements-operator.txt" "$STAGING/requirements-operator.txt"
cp -r "$PROJECT_ROOT/cli_client"     "$STAGING/cli_client"
cp -r "$PROJECT_ROOT/admin_console"  "$STAGING/admin_console"
mkdir -p "$STAGING/src"
cp -r "$PROJECT_ROOT/src/ops_cli"    "$STAGING/src/ops_cli"
cp -r "$PROJECT_ROOT/src/shared"     "$STAGING/src/shared"
touch "$STAGING/src/__init__.py"
cp -r "$PROJECT_ROOT/scripts"        "$STAGING/scripts"
cp -r "$PROJECT_ROOT/custom_code"    "$STAGING/custom_code"
cp -r "$PROJECT_ROOT/knowledge_base" "$STAGING/knowledge_base"

# Docker assets referenced by the prod compose as bind mounts
mkdir -p "$STAGING/docker/postgres" "$STAGING/docker/ollama"
cp "$PROJECT_ROOT/docker/postgres/init-curator.sh" "$STAGING/docker/postgres/init-curator.sh"
cp "$PROJECT_ROOT/docker/ollama/entrypoint.sh"     "$STAGING/docker/ollama/entrypoint.sh"

# GeoIP DB helpers (binary DBs are pulled at runtime, not shipped)
mkdir -p "$STAGING/dbs"
for f in "$PROJECT_ROOT"/dbs/*/update.sh; do
    [[ -f "$f" ]] || continue
    rel="${f#$PROJECT_ROOT/}"
    mkdir -p "$STAGING/$(dirname "$rel")"
    cp "$f" "$STAGING/$rel"
done

# Alembic (needed by operator venv-side migrations if ever run)
cp "$PROJECT_ROOT/alembic.ini" "$STAGING/alembic.ini"
cp -r "$PROJECT_ROOT/src/migrations" "$STAGING/src/migrations"

# Strip .pyc / __pycache__
find "$STAGING" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "$STAGING" -type f -name '*.pyc' -delete 2>/dev/null || true

# Ensure scripts are executable
chmod +x "$STAGING/installer.sh" \
         "$STAGING/configure-email-channel.sh" \
         "$STAGING/configure-telegram-channel.sh" \
         "$STAGING/bin/"gsage-* \
         "$STAGING/docker/postgres/init-curator.sh" \
         "$STAGING/docker/ollama/entrypoint.sh"
find "$STAGING/dbs" -name 'update.sh' -exec chmod +x {} +

# ── MANIFEST.json ─────────────────────────────────────────────────────
MANIFEST="$STAGING/MANIFEST.json"
{
    echo "{"
    echo "  \"name\": \"gsage\","
    echo "  \"version\": \"$VERSION\","
    echo "  \"registry\": \"$REGISTRY\","
    echo "  \"built_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
    echo "  \"images\": {"
    first=1
    for name in "${IMAGES[@]}"; do
        if [[ $first -eq 1 ]]; then first=0; else echo ","; fi
        printf '    "%s": { "ref": "%s/gsage-%s:%s", "digest": "%s" }' \
            "$name" "$REGISTRY" "$name" "$VERSION" "${IMAGE_DIGESTS[$name]}"
    done
    echo ""
    echo "  }"
    echo "}"
} > "$MANIFEST"

if [[ $DRY_RUN -eq 1 ]]; then
    echo ""
    echo "Dry run complete. Staging tree at: $STAGING"
    exit 0
fi

# ── Tar + sha256 (reproducible-ish) ───────────────────────────────────
echo ""
echo ">> tarring $TARBALL"
rm -f "$TARBALL" "${TARBALL}.sha256"
tar --sort=name \
    --owner=0 --group=0 --numeric-owner \
    --mtime="${SOURCE_DATE_EPOCH:-@0}" \
    -C "$OUTPUT_DIR/staging" \
    -czf "$TARBALL" "$BUNDLE_NAME"

( cd "$OUTPUT_DIR" && sha256sum "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256" )

echo ""
echo "Bundle : $TARBALL"
echo "SHA256 : $(cat "${TARBALL}.sha256")"
echo ""
echo "Done."
