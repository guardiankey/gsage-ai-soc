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
#   gsage-backend_api     → runtime-api       FastAPI backend + celery + workers
#                                             (+ chromium/node for Teams mermaid)
#   gsage-worker_tools    → runtime-tools     Celery worker + nmap/tshark/pandoc
#   gsage-mcp_server      → runtime-mermaid   MCP server + chromium + mermaid-cli
#   gsage-dev-full        → dev               Superset used by dev docker-compose
#
# Single-Dockerfile images (standalone build context):
#   gsage-frontend        (web_client/Dockerfile)   React SPA served by nginx
#   gsage-curator         (curator/Dockerfile)      Reputation list service
#
# ── Build strategy (buildx) ─────────────────────────────────────────────────
# All selected targets are built in a single `docker buildx bake` run, so
# stages shared by several images (builder, base, base-mermaid) are built
# exactly once even on a cold cache, and independent targets can be built in
# parallel by BuildKit.
#
# The layer cache is registry-backed with `type=registry,mode=max` (it also
# exports intermediate stages): when --push is used it defaults to
# <registry>/gsage-buildcache:<target>, so follow-up builds are mostly cache
# hits — on this machine, in CI, or on any host sharing the registry.
# Disable with --no-cache-registry.
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
#   --dry-run            Buildx only: generate + validate the bake file, print
#                        the bake file and exit (no build, no push).
#   --no-buildx          Use legacy `docker build` (default uses buildx with
#                        cache mounts, bake and registry cache).
#   --cache-registry <R> Registry used for the layer cache at
#                        <R>/gsage-buildcache:<target> (exported with
#                        mode=max). Defaults to <registry> when --push is
#                        used; set it to share the cache via another
#                        namespace.
#   --no-cache-registry  Do not use/update the registry-backed layer cache
#                        (inline cache embedded in the published image only).
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
# Registry used for buildx layer cache images at <R>/gsage-buildcache:<short>.
# Empty = default: when --push is used it falls back to $REGISTRY (disable
# with --no-cache-registry). Otherwise the inline cache is used.
CACHE_REGISTRY=""
# Disable the fallback to $REGISTRY for the layer cache when pushing.
NO_CACHE_REGISTRY=0
# buildx only: generate + validate the bake file, print it and exit.
DRY_RUN=0

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
    # Print the header comment block (everything before `set -euo pipefail`).
    sed -n '2,/^set -euo pipefail$/p' "$0" | sed -e 's/^# \{0,1\}//' -e '$d'
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
        --no-cache-registry) NO_CACHE_REGISTRY=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
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

# ── Registry-backed layer cache (default when pushing) ─────────────────────
# Heavy layers (python deps, chromium, texlive, npm) are exported to
# <CACHE_REGISTRY>/gsage-buildcache:<target> with mode=max, including
# intermediate stages, so follow-up builds are mostly cache hits even on a
# fresh builder or machine. Disable with --no-cache-registry.
if [[ -z "$CACHE_REGISTRY" && $NO_CACHE_REGISTRY -eq 0 && $PUSH -eq 1 && $USE_BUILDX -eq 1 ]]; then
    CACHE_REGISTRY="$REGISTRY"
fi

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
if [[ -n "$CACHE_REGISTRY" ]]; then
    echo "  Cache    : registry ($CACHE_REGISTRY/gsage-buildcache:<target>, mode=max)"
else
    echo "  Cache    : inline (no registry cache)"
fi
[[ $DRY_RUN -eq 1 ]] && echo "  Dry run  : yes (no build, no push)"
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

# ── Per-target refs / build plan ───────────────────────────────────────────
# Resolve names, contexts and tags once; the buildx path feeds them into a
# single `docker buildx bake` run (below), the legacy path into the
# sequential `docker build` loop.
declare -A TARGET_OF=() NAME_OF=() CTX_OF=() DF_OF=() FULL_TAG_OF=() LATEST_TAG_OF=()
for short in "${SELECTED[@]}"; do
    TARGET_OF[$short]="$(image_target "$short")"
    NAME_OF[$short]="$(image_name "$short")"
    IFS=$'\t' read -r ctx_dir df_path < <(image_build_context "$short")
    CTX_OF[$short]="$ctx_dir"
    DF_OF[$short]="$df_path"
    FULL_TAG_OF[$short]="$REGISTRY/${NAME_OF[$short]}:$TAG"
    LATEST_TAG_OF[$short]="$REGISTRY/${NAME_OF[$short]}:latest"
done

# Print the banner block for one target (shared by both build paths).
print_plan() {
    local short="$1"
    echo "──────────────────────────────────────────────────────────"
    echo "  Building ${NAME_OF[$short]}"
    [[ "${TARGET_OF[$short]}" != "-" ]] && echo "     target   = ${TARGET_OF[$short]}"
    echo "     context  = ${CTX_OF[$short]}"
    echo "     df       = ${DF_OF[$short]}"
    echo "     → ${FULL_TAG_OF[$short]}"
    [[ $NO_LATEST -eq 0 ]] && echo "     → ${LATEST_TAG_OF[$short]}"
    echo "──────────────────────────────────────────────────────────"
}

# ── buildx bake definition (one entry per selected target) ─────────────────
# Emitted as JSON at runtime so the image/target mapping stays defined only in
# this script. A single bake graph lets BuildKit build stages shared by
# several images (builder/base/base-mermaid) exactly once even on a cold
# cache, and schedule independent targets in parallel.
bake_cache_flags_of() {
    # Output: "<cache-from json>;<cache-to json>;<build-args json or empty>"
    local short="$1"
    local prev_img="${FULL_TAG_OF[$short]}"
    [[ $NO_LATEST -eq 0 ]] && prev_img="${LATEST_TAG_OF[$short]}"
    if [[ -n "$CACHE_REGISTRY" ]]; then
        local cache_ref="$CACHE_REGISTRY/gsage-buildcache:$short"
        # Prefer the dedicated buildcache, but also accept layers from the
        # last published image (covers the first run, before the cache image
        # exists yet).
        printf '["type=registry,ref=%s", "type=registry,ref=%s"];["type=registry,ref=%s,mode=max"];\n' \
            "$cache_ref" "$prev_img" "$cache_ref"
    else
        # No registry cache: inline cache embedded in the pushed image.
        printf '["type=registry,ref=%s"];["type=inline"];{"BUILDKIT_INLINE_CACHE": "1"}\n' "$prev_img"
    fi
}

bake_target_json() {
    local short="$1" rel_df cache_from cache_to build_args
    rel_df="$(realpath --relative-to="${CTX_OF[$short]}" "${DF_OF[$short]}")"
    IFS=';' read -r cache_from cache_to build_args < <(bake_cache_flags_of "$short")

    printf '    "%s": {\n' "$short"
    printf '      "context": "%s",\n' "${CTX_OF[$short]}"
    printf '      "dockerfile": "%s",\n' "$rel_df"
    [[ "${TARGET_OF[$short]}" != "-" ]] && printf '      "target": "%s",\n' "${TARGET_OF[$short]}"
    printf '      "tags": ["%s"' "${FULL_TAG_OF[$short]}"
    [[ $NO_LATEST -eq 0 ]] && printf ', "%s"' "${LATEST_TAG_OF[$short]}"
    printf '],\n'
    printf '      "cache-from": %s,\n' "$cache_from"
    printf '      "cache-to": %s' "$cache_to"
    [[ -n "$build_args" ]] && printf ',\n      "args": %s' "$build_args"
    printf '\n    }'
}

write_bake_file() {
    local out="$1" short sep=""
    {
        printf '{\n  "target": {\n'
        for short in "${SELECTED[@]}"; do
            printf '%s' "$sep"
            bake_target_json "$short"
            sep=$',\n'
        done
        printf '\n  }\n}\n'
    } > "$out"
}

# ── Build ──────────────────────────────────────────────────────────────────
if [[ $USE_BUILDX -eq 1 ]]; then
    # buildx: single bake run for every selected target.
    for short in "${SELECTED[@]}"; do
        print_plan "$short"
    done

    BAKE_FILE="${TMPDIR:-/tmp}/gsage-bake-$$-${RANDOM}.json"
    cleanup_bake_file() {
        # Keep the generated file on --dry-run so it can be inspected/reused.
        [[ $DRY_RUN -eq 1 ]] || rm -f "$BAKE_FILE"
    }
    trap cleanup_bake_file EXIT

    write_bake_file "$BAKE_FILE"

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  Dry run — generated bake file: $BAKE_FILE"
        sed 's/^/    /' "$BAKE_FILE"
        echo ""
        if docker buildx bake -f "$BAKE_FILE" "${SELECTED[@]}" --print >/dev/null; then
            echo "  ✔ bake file validated (docker buildx bake --print)"
        else
            echo "  ERROR: bake file failed validation" >&2
            exit 1
        fi
        exit 0
    fi

    # buildx: push directly to the registry, or load into the local daemon.
    # The flag applies to every target in the bake graph.
    bake_flags=()
    if [[ $PUSH -eq 1 ]]; then
        bake_flags+=(--push)
    else
        bake_flags+=(--load)
    fi

    docker buildx bake -f "$BAKE_FILE" "${bake_flags[@]}" "${SELECTED[@]}"

    echo ""
else
    # ── Legacy path (--no-buildx): sequential docker build + docker push ───
    for short in "${SELECTED[@]}"; do
        print_plan "$short"

        docker_args=(build -f "${DF_OF[$short]}")
        [[ "${TARGET_OF[$short]}" != "-" ]] && docker_args+=(--target "${TARGET_OF[$short]}")
        docker_args+=(-t "${FULL_TAG_OF[$short]}")
        [[ $NO_LATEST -eq 0 ]] && docker_args+=(-t "${LATEST_TAG_OF[$short]}")
        docker_args+=("${CTX_OF[$short]}")

        if [[ $DRY_RUN -eq 1 ]]; then
            echo "  (dry run) docker ${docker_args[*]}"
            [[ $PUSH -eq 1 ]] && echo "  (dry run) docker push ${FULL_TAG_OF[$short]}"
            echo ""
            continue
        fi

        docker "${docker_args[@]}"

        if [[ $PUSH -eq 1 ]]; then
            echo ""
            echo "  Pushing ${FULL_TAG_OF[$short]} …"
            if ! docker push "${FULL_TAG_OF[$short]}"; then
                FAILED_PUSH=1
                echo "  ERROR: push failed for ${FULL_TAG_OF[$short]}" >&2
                continue
            fi
            if [[ $NO_LATEST -eq 0 ]]; then
                echo "  Pushing ${LATEST_TAG_OF[$short]} …"
                if ! docker push "${LATEST_TAG_OF[$short]}"; then
                    FAILED_PUSH=1
                    echo "  ERROR: push failed for ${LATEST_TAG_OF[$short]}" >&2
                fi
            fi
        fi

        echo ""
    done
fi

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
