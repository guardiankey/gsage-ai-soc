#!/usr/bin/env bash
# installer.sh — gSage AI host-side installer (Debian/RHEL family, x86_64/arm64).
#
# Bundled layout (created by scripts_operations/build-release-bundle.sh):
#   installer.sh, wizard.lib.sh, preflight.lib.sh, VERSION, MANIFEST.json,
#   env.template, compose/docker-compose.yml,
#   configure-email-channel.sh, configure-telegram-channel.sh, configure-teams-channel.sh,
#   bin/{gsage-cli,gsage-admin,gsage-get-admin-key},
#   requirements-operator.txt, cli_client/, admin_console/, src/..., scripts/,
#   custom_code/, knowledge_base/, docker/{postgres,ollama}/, dbs/*/update.sh
#
# Target layout produced by this script:
#   /opt/gsage/releases/<ver>/        ← extracted bundle
#   /opt/gsage/current            ← symlink to the active release
#   /opt/gsage/shared/
#       .env                      ← 0600 root:root, never overwritten on upgrade
#       operator-venv/            ← host Python venv for gsage-cli / gsage-admin
#       logs/{install,helpers}/
#       dbs/, knowledge_base/, custom_code/
#   /usr/local/bin/{gsage-cli,gsage-admin,gsage-get-admin-key,
#                   gsage-configure-email,gsage-configure-telegram,gsage-configure-teams}

set -euo pipefail

# ── Ensure a UTF-8 locale ────────────────────────────────────────
# Many .env values, prompts and Python (Rich / Textual) outputs use UTF-8
# characters and bash sourcing of the rendered .env breaks under C/POSIX.
# Honour the user's existing locale when it is already UTF-8; only fall back
# to C.UTF-8 (or the UTF-8 variant of the same language) when needed; abort
# with a clear error if no UTF-8 locale exists on the system.
_gsage_ensure_utf8() {
    local current="${LC_ALL:-${LANG:-}}"
    if [[ "$current" =~ [Uu][Tt][Ff]-?8 ]]; then
        export PYTHONIOENCODING=utf-8
        return 0
    fi
    local available candidate lang_only
    available="$(locale -a 2>/dev/null || true)"
    if [[ -n "$current" && "$current" != "C" && "$current" != "POSIX" ]]; then
        lang_only="${current%%.*}"
        for candidate in "${lang_only}.UTF-8" "${lang_only}.utf8"; do
            if printf '%s\n' "$available" | grep -qiFx "$candidate"; then
                export LANG="$candidate" LC_ALL="$candidate" PYTHONIOENCODING=utf-8
                return 0
            fi
        done
    fi
    for candidate in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8; do
        if printf '%s\n' "$available" | grep -qiFx "$candidate"; then
            export LANG="$candidate" LC_ALL="$candidate" PYTHONIOENCODING=utf-8
            return 0
        fi
    done
    cat >&2 <<'EOF'
ERROR: the gSage installer requires a UTF-8 locale to render prompts and
       write .env values correctly, but no UTF-8 locale was found.
       Install one before re-running the installer, e.g.:
         Debian/Ubuntu:  apt-get install -y locales && \
                         locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8
         RHEL/Fedora:    dnf install -y glibc-langpack-en
         Alpine/musl:    apk add --no-cache musl-locales musl-locales-lang
EOF
    return 1
}
_gsage_ensure_utf8 || exit 1

# ── Pipe-safe: if we're being read from `curl | bash`, re-exec with a TTY. ──
if [[ ! -t 0 && -r /dev/tty ]]; then
    exec bash "${BASH_SOURCE[0]}" "$@" </dev/tty
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./preflight.lib.sh
. "$SCRIPT_DIR/preflight.lib.sh"
# shellcheck source=./wizard.lib.sh
. "$SCRIPT_DIR/wizard.lib.sh"

GSAGE_ROOT="${GSAGE_ROOT:-/opt/gsage}"
GSAGE_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/VERSION")"
RELEASE_DIR="$GSAGE_ROOT/releases/$GSAGE_VERSION"
CURRENT_LINK="$GSAGE_ROOT/current"
SHARED_DIR="$GSAGE_ROOT/shared"
ENV_FILE="$SHARED_DIR/.env"
LOG_DIR_TMP="/tmp"
LOG_DIR_FINAL="$SHARED_DIR/logs/install"
LOG_TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE_TMP="$LOG_DIR_TMP/gsage-install-$LOG_TS.log"

# Tee everything (stdout + stderr) to the install log.
exec > >(tee -a "$LOG_FILE_TMP") 2>&1

echo "════════════════════════════════════════════════════════════"
echo "  gSage AI installer — version $GSAGE_VERSION"
echo "  started at $(date -u +%FT%TZ)"
echo "  log: $LOG_FILE_TMP"
echo "════════════════════════════════════════════════════════════"

# ── 1. Preflight ─────────────────────────────────────────────────────
preflight::check_root
preflight::detect_os
preflight::check_arch
preflight::check_tools
mkdir -p "$GSAGE_ROOT"
preflight::check_disk "$GSAGE_ROOT" 20
preflight::check_ram 4 8
preflight::ensure_docker
preflight::ensure_python

# ── 2. Re-run / upgrade detection ────────────────────────────────────
if [[ -L "$CURRENT_LINK" && -f "$CURRENT_LINK/VERSION" ]]; then
    existing="$(tr -d '[:space:]' < "$CURRENT_LINK/VERSION" || echo unknown)"
    if [[ "$existing" == "$GSAGE_VERSION" ]]; then
        echo ""
        echo "gSage $existing is already installed at $CURRENT_LINK."
        echo "Nothing to do. If you want to reconfigure, edit $ENV_FILE and run:"
        echo "    cd $CURRENT_LINK && docker compose --env-file $ENV_FILE -f compose/docker-compose.yml up -d"
        exit 0
    fi
    # Compare versions (lexical is fine for semver within same prefix; do a conservative test).
    lower="$(printf '%s\n%s\n' "$existing" "$GSAGE_VERSION" | sort -V | head -1)"
    if [[ "$lower" == "$GSAGE_VERSION" && "$existing" != "$GSAGE_VERSION" ]]; then
        echo "ERROR: existing install is $existing, bundle is $GSAGE_VERSION (downgrade not supported)." >&2
        exit 3
    fi
    echo ""
    echo "Upgrading $existing → $GSAGE_VERSION. .env and named volumes will be preserved."
    UPGRADE_MODE=1
else
    UPGRADE_MODE=0
fi

# ── 3. Shared directories ────────────────────────────────────────────
mkdir -p "$SHARED_DIR" \
         "$SHARED_DIR/logs/install" \
         "$SHARED_DIR/logs/helpers" \
         "$SHARED_DIR/dbs" \
         "$SHARED_DIR/knowledge_base" \
         "$SHARED_DIR/custom_code"
chmod 0750 "$SHARED_DIR"

# Seed knowledge_base and custom_code from the bundle only if empty.
if [[ -z "$(ls -A "$SHARED_DIR/knowledge_base" 2>/dev/null || true)" && -d "$SCRIPT_DIR/knowledge_base" ]]; then
    cp -r "$SCRIPT_DIR/knowledge_base/." "$SHARED_DIR/knowledge_base/"
fi
if [[ -z "$(ls -A "$SHARED_DIR/custom_code" 2>/dev/null || true)" && -d "$SCRIPT_DIR/custom_code" ]]; then
    cp -r "$SCRIPT_DIR/custom_code/." "$SHARED_DIR/custom_code/"
fi
if [[ -z "$(ls -A "$SHARED_DIR/enterprise" 2>/dev/null || true)" && -d "$SCRIPT_DIR/enterprise" ]]; then
    cp -r "$SCRIPT_DIR/enterprise/." "$SHARED_DIR/enterprise/"
fi

# ── 4. Wizard (only on fresh install) ────────────────────────────────
if [[ $UPGRADE_MODE -eq 0 || ! -f "$ENV_FILE" ]]; then
    # Pre-export so the wizard picks them up as defaults.
    export GSAGE_VERSION
    export GSAGE_IMAGE_REGISTRY="$(python3 -c '
import json,sys
with open(sys.argv[1]) as f: print(json.load(f).get("registry","guardiankey"))
' "$SCRIPT_DIR/MANIFEST.json" 2>/dev/null || echo guardiankey)"
    export GSAGE_INSTALL_DIR="$CURRENT_LINK"
    export GSAGE_DBS_PATH="$SHARED_DIR/dbs"
    export GSAGE_KB_PATH="$SHARED_DIR/knowledge_base"
    export GSAGE_CUSTOM_CODE_PATH="$SHARED_DIR/custom_code"
    export GSAGE_ENTERPRISE_PATH="$SHARED_DIR/enterprise"

    # Pre-flight port availability using the default the wizard will suggest.
    preflight::check_ports 8080 || {
        echo "   Free the port or change it in the wizard when prompted." >&2
    }

    wizard::run
    wizard::render_env "$SCRIPT_DIR/env.template" "$ENV_FILE"
    # Leave the raw template as .env.example for reference (no secrets — only placeholders).
    install -m 0644 "$SCRIPT_DIR/env.template" "$SHARED_DIR/.env.example"
else
    echo ""
    echo "Existing .env preserved: $ENV_FILE"
    # Always refresh the example so it stays in sync with the installed version.
    [[ -f "$SCRIPT_DIR/env.template" ]] && install -m 0644 "$SCRIPT_DIR/env.template" "$SHARED_DIR/.env.example"
fi

# ── 5. Extract release tree ──────────────────────────────────────────
echo ""
echo ">> staging release at $RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
# Copy bundle contents (but not the original tarball / staging); we're already
# running from the extracted tree.
cp -a "$SCRIPT_DIR"/. "$RELEASE_DIR"/
# The release tree shouldn't carry the install log.
rm -f "$RELEASE_DIR/$(basename "$LOG_FILE_TMP")" 2>/dev/null || true

# Atomic symlink flip.
tmp_link="$GSAGE_ROOT/.current.new"
ln -sfn "$RELEASE_DIR" "$tmp_link"
mv -Tf "$tmp_link" "$CURRENT_LINK"
echo "Active release: $CURRENT_LINK → $RELEASE_DIR"

# Each service in docker-compose.yml declares `env_file: .env` relative to the
# compose directory, so docker compose expects compose/.env next to the yml.
# Point it to the single source of truth at $ENV_FILE via a symlink.
ln -sfn "$ENV_FILE" "$RELEASE_DIR/compose/.env"

# ── 6. Operator venv (host-side Python) ──────────────────────────────
VENV_DIR="$SHARED_DIR/operator-venv"
if [[ ! -d "$VENV_DIR" ]]; then
    echo ""
    echo ">> creating operator venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
# Always upgrade pip + (re)install in case requirements changed on upgrade.
"$VENV_DIR/bin/pip" install --quiet --upgrade pip wheel
"$VENV_DIR/bin/pip" install --quiet -r "$CURRENT_LINK/requirements-operator.txt"
echo "Operator venv ready."

# ── 7. Host wrappers ─────────────────────────────────────────────────
install -m 0755 "$CURRENT_LINK/bin/gsage-cli"           /usr/local/bin/gsage-cli
install -m 0755 "$CURRENT_LINK/bin/gsage-admin"         /usr/local/bin/gsage-admin
install -m 0755 "$CURRENT_LINK/bin/gsage-get-admin-key" /usr/local/bin/gsage-get-admin-key
# Channel-configuration helpers exposed under friendlier names. They are
# symlinked (rather than copied) so they always track the active release.
ln -sfn "$CURRENT_LINK/configure-email-channel.sh"    /usr/local/bin/gsage-configure-email
ln -sfn "$CURRENT_LINK/configure-telegram-channel.sh" /usr/local/bin/gsage-configure-telegram
ln -sfn "$CURRENT_LINK/configure-teams-channel.sh"    /usr/local/bin/gsage-configure-teams
echo "Installed: /usr/local/bin/gsage-{cli,admin,get-admin-key,configure-email,configure-telegram,configure-teams}"

# ── 8. Bring the stack up ────────────────────────────────────────────
echo ""
echo ">> pulling images (this may take a while; progress shown only on errors)"
( cd "$CURRENT_LINK" && docker compose --progress quiet --env-file "$ENV_FILE" -f compose/docker-compose.yml pull )

echo ""
echo ">> starting services"
( cd "$CURRENT_LINK" && docker compose --progress quiet --env-file "$ENV_FILE" -f compose/docker-compose.yml up -d )

echo ""
echo ">> waiting for backend_api to be healthy (up to 5 minutes)"
deadline=$(( $(date +%s) + 300 ))
while :; do
    state="$(docker inspect --format '{{.State.Health.Status}}' gsage-backend_api 2>/dev/null || echo starting)"
    if [[ "$state" == "healthy" ]]; then
        echo "   backend_api: healthy"
        break
    fi
    if (( $(date +%s) >= deadline )); then
        echo "WARN: backend_api did not become healthy within 5 minutes. Check logs:"
        echo "    docker compose -f $CURRENT_LINK/compose/docker-compose.yml logs backend_api"
        break
    fi
    sleep 5
done

# ── 9. Capture the bootstrap API key from backend logs ───────────────
echo ""
echo ">> capturing bootstrap admin key"
ADMIN_KEY="$(docker logs gsage-backend_api 2>&1 | grep -oE 'gk_live_[A-Za-z0-9_-]+' | head -1 || true)"

# ── 10. Finalise logs ────────────────────────────────────────────────
mkdir -p "$LOG_DIR_FINAL"
mv "$LOG_FILE_TMP" "$LOG_DIR_FINAL/install-$LOG_TS.log" 2>/dev/null || true
LOG_FILE_FINAL="$LOG_DIR_FINAL/install-$LOG_TS.log"

# ── 11. Summary ──────────────────────────────────────────────────────
cat <<EOF

════════════════════════════════════════════════════════════
  gSage AI $GSAGE_VERSION installed.
════════════════════════════════════════════════════════════

  Web UI     : http://$(hostname -I | awk '{print $1}'):${WIZARD_ANS[frontend_port]:-8080}
  API        : http://$(hostname -I | awk '{print $1}'):${WIZARD_ANS[frontend_port]:-8080}/api/
                 (backend_api is reached through the frontend reverse proxy)

  Admin email    : ${WIZARD_ANS[admin_email]:-(preserved from existing .env)}
  Admin password : (as set in the wizard — stored only in $ENV_FILE)
  Admin API key  : ${ADMIN_KEY:-(check backend logs if not printed)}

  Host commands (installed under /usr/local/bin):
    gsage-cli                  REST CLI client (argparse-based; scripting-friendly)
    gsage-admin                Textual admin console (interactive TUI)
    gsage-get-admin-key        Reprint or rotate the bootstrap admin API key
    gsage-configure-email      Guided IMAP/SMTP mailbox setup for an organization
    gsage-configure-telegram   Guided Telegram bot channel setup for an organization
    gsage-configure-teams      Guided Microsoft Teams bot channel setup for an organization

    Tip: list them with   ls /usr/local/bin/gsage-*

  Next steps:
    # Configure the inbound email channel (IMAP/SMTP):
    sudo gsage-configure-email

    # Configure a Telegram bot channel:
    sudo gsage-configure-telegram

    # Configure a Microsoft Teams bot channel:
    sudo gsage-configure-teams

  Installation log: $LOG_FILE_FINAL

EOF
