#!/usr/bin/env bash
# publish-images.sh — Build (and optionally push) gSage AI runtime images.
#
# Builds runtime images and tags them as <registry>/<image>:<tag> plus
# <registry>/<image>:latest. Push is opt-in via --push and assumes
# `docker login <registry>` was done.
#
# ── Image / target mapping ──────────────────────────────────────────────────
#
# Multi-target images (all built from docker/Dockerfile):
#   gsage-backend_api     → runtime-minimal   FastAPI backend + celery + workers
#   gsage-worker_tools    → runtime-tools     Celery worker + nmap/tshark/pandoc
#   gsage-mcp_server      → runtime-mermaid   MCP server + chromium + mermaid-cli
#   gsage-dev-full        → dev               Superset used by dev docker-compose
#
# Single-Dockerfile images (standalone build context):
#   gsage-frontend        (web_client/Dockerfile)   React SPA served by nginx
#   gsage-curator         (curator/Dockerfile)      Reputation list service
#
# ── Usage ───────────────────────────────────────────────────────────────────
#   bash scripts_operations/publish-images.sh \
#        --registry docker.io/guardiankey \
#        --tag 0.1.0 \
#        [--target backend_api,mcp_server,frontend] \
#        [--no-latest] \
#        [--push]
#
# Required:
#   --registry <REG>     Registry namespace, e.g. docker.io/guardiankey
#
# Optional:
#   --tag <TAG>          Version tag. Default: content of ./VERSION.
#                        Also published as `latest` unless --no-latest.
#   --target <list>      CSV of targets to build. Default: all runtime targets
#                        (excludes dev-full). Valid: backend_api, worker_tools,
#                        mcp_server, frontend, curator, dev-full.
#   --no-latest          Skip the extra `:latest` tag.
#   --push               Push images to the registry after build.
#   -h | --help          Show this help.
#
# Authentication:
#   The script assumes you already ran `docker login <registry>`. If push
#   fails with an auth error, you will be prompted to run it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE="$PROJECT_ROOT/docker/Dockerfile"
VERSION_FILE="$PROJECT_ROOT/VERSION"

# ── Argument defaults ──────────────────────────────────────────────────────
REGISTRY=""
TAG=""
TARGETS_ARG=""
PUSH=0
NO_LATEST=0

# All runtime targets published by default (dev-full is opt-in).
DEFAULT_TARGETS=(backend_api worker_tools mcp_server frontend curator)
ALL_TARGETS=(backend_api worker_tools mcp_server frontend curator dev-full)

# image short-name → dockerfile target inside docker/Dockerfile
# Returns "-" for images that have their own Dockerfile (no --target).
image_target() {
    case "$1" in
        backend_api)  echo "runtime-minimal" ;;
        worker_tools) echo "runtime-tools" ;;
        mcp_server)   echo "runtime-mermaid" ;;
        dev-full)     echo "dev" ;;
        frontend)     echo "-" ;;
        curator)      echo "-" ;;
        *) return 1 ;;
    esac
}

# image short-name → published image name (no registry prefix)
image_name() {
    case "$1" in
        backend_api)  echo "gsage-backend_api" ;;
        worker_tools) echo "gsage-worker_tools" ;;
        mcp_server)   echo "gsage-mcp_server" ;;
        dev-full)     echo "gsage-dev-full" ;;
        frontend)     echo "gsage-frontend" ;;
        curator)      echo "gsage-curator" ;;
        *) return 1 ;;
    esac
}

# image short-name → docker build context + dockerfile (relative to repo root).
# Output: "<context-dir>\t<dockerfile-path>"
image_build_context() {
    case "$1" in
        backend_api|worker_tools|mcp_server|dev-full)
            printf '%s\t%s\n' "$PROJECT_ROOT" "$DOCKERFILE"
            ;;
        frontend)
            printf '%s\t%s\n' "$PROJECT_ROOT/web_client" "$PROJECT_ROOT/web_client/Dockerfile"
            ;;
        curator)
            # Curator Dockerfile COPY paths are relative to repo root.
            printf '%s\t%s\n' "$PROJECT_ROOT" "$PROJECT_ROOT/curator/Dockerfile"
            ;;
        *) return 1 ;;
    esac
}

usage() {
    sed -n '1,42p' "$0" | sed -n 's/^# \{0,1\}//p'
    exit "${1:-0}"
}

# ── Parse args ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --registry) REGISTRY="${2:-}"; shift 2 ;;
        --tag)      TAG="${2:-}";      shift 2 ;;
        --target)   TARGETS_ARG="${2:-}"; shift 2 ;;
        --push)     PUSH=1; shift ;;
        --no-latest) NO_LATEST=1; shift ;;
        -h|--help)  usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

# ── Validate ───────────────────────────────────────────────────────────────
[[ -z "$REGISTRY" ]] && { echo "ERROR: --registry is required" >&2; usage 1; }

# Default tag = content of VERSION file.
if [[ -z "$TAG" ]]; then
    if [[ -f "$VERSION_FILE" ]]; then
        TAG="$(tr -d '[:space:]' < "$VERSION_FILE")"
    fi
fi
[[ -z "$TAG" ]] && { echo "ERROR: --tag is required (or populate VERSION file)" >&2; usage 1; }

# strip trailing slash on registry
REGISTRY="${REGISTRY%/}"

# Build list of targets to process.
if [[ -z "$TARGETS_ARG" ]]; then
    SELECTED=("${DEFAULT_TARGETS[@]}")
else
    IFS=',' read -r -a SELECTED <<< "$TARGETS_ARG"
    for t in "${SELECTED[@]}"; do
        if ! image_target "$t" >/dev/null 2>&1; then
            echo "ERROR: unknown target '$t'. Valid: ${ALL_TARGETS[*]}" >&2
            exit 1
        fi
    done
fi

echo "══════════════════════════════════════════════════════════"
echo "  Registry : $REGISTRY"
echo "  Tag      : $TAG $([[ $NO_LATEST -eq 0 ]] && echo '+ latest')"
echo "  Targets  : ${SELECTED[*]}"
echo "  Push     : $([[ $PUSH -eq 1 ]] && echo 'yes' || echo 'no (build only)')"
echo "══════════════════════════════════════════════════════════"
echo ""

# ── Sync docs into knowledge_base/gsage before building ───────────────────
# These files are baked into the runtime images via COPY in the Dockerfile.
KB_GSAGE="$PROJECT_ROOT/knowledge_base/default/gsage"
declare -A KB_SOURCES=(
    ["README.md"]="$PROJECT_ROOT/README.md"
    ["TOOLS.md"]="$PROJECT_ROOT/docs/dev/TOOLS.md"
    ["LICENSE.md"]="$PROJECT_ROOT/LICENSE.md"
)

echo "  Syncing docs to knowledge_base/gsage/default/ …"
mkdir -p "$KB_GSAGE"
for dest_name in "${!KB_SOURCES[@]}"; do
    src="${KB_SOURCES[$dest_name]}"
    if [[ -f "$src" ]]; then
        cp -f "$src" "$KB_GSAGE/$dest_name"
        echo "    copied $(basename "$src") → knowledge_base/gsage/default/$dest_name"
    else
        echo "    WARNING: source not found, skipping: $src" >&2
    fi
done
echo ""

FAILED_PUSH=0

# ── Build + tag + push loop ────────────────────────────────────────────────
for short in "${SELECTED[@]}"; do
    target="$(image_target "$short")"
    base_name="$(image_name "$short")"
    IFS=$'\t' read -r ctx_dir df_path < <(image_build_context "$short")
    full_tag="$REGISTRY/$base_name:$TAG"
    latest_tag="$REGISTRY/$base_name:latest"

    echo "──────────────────────────────────────────────────────────"
    echo "  Building $base_name"
    [[ "$target" != "-" ]] && echo "     target   = $target"
    echo "     context  = $ctx_dir"
    echo "     df       = $df_path"
    echo "     → $full_tag"
    [[ $NO_LATEST -eq 0 ]] && echo "     → $latest_tag"
    echo "──────────────────────────────────────────────────────────"

    build_args=(build -f "$df_path")
    [[ "$target" != "-" ]] && build_args+=(--target "$target")
    build_args+=(-t "$full_tag")
    [[ $NO_LATEST -eq 0 ]] && build_args+=(-t "$latest_tag")
    build_args+=("$ctx_dir")

    docker "${build_args[@]}"

    if [[ $PUSH -eq 1 ]]; then
        echo ""
        echo "  Pushing $full_tag …"
        if ! docker push "$full_tag"; then
            FAILED_PUSH=1
            echo "  ERROR: push failed for $full_tag" >&2
            continue
        fi
        if [[ $NO_LATEST -eq 0 ]]; then
            echo "  Pushing $latest_tag …"
            if ! docker push "$latest_tag"; then
                FAILED_PUSH=1
                echo "  ERROR: push failed for $latest_tag" >&2
            fi
        fi
    fi

    echo ""
done

echo "══════════════════════════════════════════════════════════"
if [[ $FAILED_PUSH -eq 1 ]]; then
    echo "  Some pushes failed."
    echo "  If the error mentioned authentication, run:"
    echo "      docker login $REGISTRY"
    echo "  and re-run this script with --push."
    exit 2
fi

if [[ $PUSH -eq 1 ]]; then
    echo "  Done. Images built and pushed to $REGISTRY."
else
    echo "  Done. Images built locally. Re-run with --push to publish."
fi
echo "══════════════════════════════════════════════════════════"
