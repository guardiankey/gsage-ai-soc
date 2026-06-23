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
#   # Development build (default) — tags images as <registry>/<image>:dev
#   bash scripts_operations/publish-images.sh --registry docker.io/guardiankey
#
#   # Production release — tags with version from ./VERSION + :latest
#   bash scripts_operations/publish-images.sh \
#        --registry docker.io/guardiankey \
#        --production
#
#   # Explicit tag override (always tags :latest unless --no-latest)
#   bash scripts_operations/publish-images.sh \
#        --registry docker.io/guardiankey \
#        --tag 0.1.0
#
#   # Full example
#   bash scripts_operations/publish-images.sh \
#        --registry docker.io/guardiankey \
#        --production \
#        --target backend_api,mcp_server,frontend \
#        --push
#
# Required:
#   --registry <REG>     Registry namespace, e.g. docker.io/guardiankey
#
# Optional:
#   --production          Tag images with the version from ./VERSION and also
#                         publish :latest.  Without this flag the default tag
#                         is "dev" and :latest is NOT published (safety).
#   --tag <TAG>          Explicit version tag.  Overrides both --production
#                         and the default "dev".  Still publishes :latest
#                         unless --no-latest.
#   --target <list>      CSV of targets to build. Default: all runtime targets
#                        (excludes dev-full). Valid: backend_api, worker_tools,
#                        mcp_server, frontend, curator, dev-full.
#   --no-latest          Skip the extra `:latest` tag.
#   --push               Push images to the registry after build.
#   --no-buildx          Use legacy `docker build` (default uses buildx with
#                        cache mounts, parallel stages and registry cache).
#   --cache-registry <R> When using buildx, also publish a registry-backed
#                        layer cache at <R>/gsage-buildcache:<short>. Useful
#                        in CI to share heavy layers (texlive, chromium)
#                        across machines. Without this flag the script falls
#                        back to inline cache embedded in the published image.
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
PRODUCTION=0
NO_LATEST=0
# Use buildx by default (gives cache mounts, registry cache, parallel stages).
# Override with --no-buildx for environments where buildx is unavailable.
USE_BUILDX=1
# When set (and using buildx), publish/consume a registry cache image
# `<registry>/gsage-buildcache:<short>` to share layers across machines/CI.
# Empty = inline cache only (cache embedded in the image manifest, no extra image).
CACHE_REGISTRY=""

# All runtime targets published by default (dev-full is opt-in).
DEFAULT_TARGETS=(backend_api worker_tools mcp_server frontend curator)
ALL_TARGETS=(backend_api worker_tools mcp_server frontend curator dev-full)

# image short-name → dockerfile target inside docker/Dockerfile
# Returns "-" for images that have their own Dockerfile (no --target).
image_target() {
    case "$1" in
        backend_api)  echo "runtime-api" ;;
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
    sed -n '1,68p' "$0" | sed -n 's/^# \{0,1\}//p'
    exit "${1:-0}"
}

# ── Parse args ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --registry)     REGISTRY="${2:-}"; shift 2 ;;
        --tag)          TAG="${2:-}";      shift 2 ;;
        --target)       TARGETS_ARG="${2:-}"; shift 2 ;;
        --production)   PRODUCTION=1; shift ;;
        --push)         PUSH=1; shift ;;
        --no-latest)    NO_LATEST=1; shift ;;
        --no-buildx)    USE_BUILDX=0; shift ;;
        --cache-registry) CACHE_REGISTRY="${2:-}"; shift 2 ;;
        -h|--help)      usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

# ── Resolve tag ────────────────────────────────────────────────────────────
# Priority: 1) explicit --tag, 2) --production → VERSION file, 3) default "dev".
if [[ -n "$TAG" ]]; then
    :  # explicit --tag — use as-is, latest is published (unless --no-latest)
elif [[ $PRODUCTION -eq 1 ]]; then
    if [[ -f "$VERSION_FILE" ]]; then
        TAG="$(tr -d '[:space:]' < "$VERSION_FILE")"
    fi
    [[ -z "$TAG" ]] && { echo "ERROR: VERSION file not found or empty" >&2; exit 1; }
else
    TAG="dev"
fi

# Safety: dev images must never be tagged :latest.
# --production or explicit --tag imply intent to publish a release.
if [[ "$TAG" == "dev" && $NO_LATEST -eq 0 ]]; then
    echo "  ℹ  Tag is 'dev' — :latest will NOT be published (safety)." >&2
    echo "     Use --production to publish a versioned release with :latest." >&2
    NO_LATEST=1
fi

# ── Validate ───────────────────────────────────────────────────────────────
[[ -z "$REGISTRY" ]] && { echo "ERROR: --registry is required" >&2; usage 1; }

# strip trailing slash on registry
REGISTRY="${REGISTRY%/}"
[[ -n "$CACHE_REGISTRY" ]] && CACHE_REGISTRY="${CACHE_REGISTRY%/}"

# ── Ensure buildx builder exists (when enabled) ────────────────────────────
if [[ $USE_BUILDX -eq 1 ]]; then
    if ! docker buildx version >/dev/null 2>&1; then
        echo "WARNING: docker buildx not available, falling back to legacy build" >&2
        USE_BUILDX=0
    else
        # Prefer existing 'gsage' builder; create with docker-container driver if absent.
        if ! docker buildx inspect gsage >/dev/null 2>&1; then
            echo "  Creating buildx builder 'gsage' (docker-container driver) …"
            docker buildx create --name gsage --driver docker-container --use >/dev/null
        else
            docker buildx use gsage >/dev/null
        fi
        # Boot the builder so the first build doesn't pay the cold-start cost.
        docker buildx inspect --bootstrap >/dev/null
    fi
fi
# Force BuildKit even on the legacy code path so cache mounts (--mount=type=cache)
# in the Dockerfiles are honored.
export DOCKER_BUILDKIT=1

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
if [[ $PRODUCTION -eq 1 ]]; then
    echo "  Mode     : production (tag from VERSION file)"
else
    echo "  Mode     : development"
fi
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

    if [[ $USE_BUILDX -eq 1 ]]; then
        # buildx: enable inline cache + optional registry cache for cross-machine reuse.
        build_args=(buildx "${build_args[@]}")
        build_args+=(--build-arg BUILDKIT_INLINE_CACHE=1)
        if [[ -n "$CACHE_REGISTRY" ]]; then
            cache_ref="$CACHE_REGISTRY/gsage-buildcache:$short"
            build_args+=(--cache-from "type=registry,ref=$cache_ref")
            build_args+=(--cache-to   "type=registry,ref=$cache_ref,mode=max")
        else
            # Inline cache: layers are embedded in the image manifest itself.
            # Effective only when --push is used (cache lives in the registry image).
            build_args+=(--cache-to "type=inline,mode=max")
            # Reuse layers from previously published image.
            # Prefer :latest for cache (wider reuse across builds); fall back
            # to the exact tag when :latest is not being published.
            if [[ $NO_LATEST -eq 0 ]]; then
                cache_from_ref="$latest_tag"
            else
                cache_from_ref="$full_tag"
            fi
            build_args+=(--cache-from "type=registry,ref=$cache_from_ref")
        fi
        # buildx: push and load are mutually exclusive. Push directly to skip the
        # local daemon round-trip (faster I/O); otherwise load into local docker.
        if [[ $PUSH -eq 1 ]]; then
            build_args+=(--push)
        else
            build_args+=(--load)
        fi
    fi
    build_args+=("$ctx_dir")

    docker "${build_args[@]}"

    if [[ $PUSH -eq 1 && $USE_BUILDX -eq 0 ]]; then
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
    if [[ $PRODUCTION -eq 1 ]]; then
        echo "  Done. Production images ($TAG) built and pushed to $REGISTRY."
    else
        echo "  Done. Development images ($TAG) built and pushed to $REGISTRY."
    fi
else
    if [[ $PRODUCTION -eq 1 ]]; then
        echo "  Done. Production images ($TAG) built locally. Re-run with --push to publish."
    else
        echo "  Done. Development images ($TAG) built locally. Re-run with --push to publish."
    fi
fi
echo "══════════════════════════════════════════════════════════"
