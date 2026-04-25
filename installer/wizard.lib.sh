#!/usr/bin/env bash
# wizard.lib.sh — interactive wizard + .env renderer for the gSage installer.
# Sourced by installer.sh; not meant to run standalone.

# Global answers map populated by wizard::run and consumed by wizard::render_env.
declare -gA WIZARD_ANS=()

wizard::_ask() {
    # $1 = key, $2 = label, $3 = default (optional)
    local key="$1" label="$2" default="${3:-}" ans
    if [[ -n "$default" ]]; then
        read -r -p "$label [$default]: " ans </dev/tty
        ans="${ans:-$default}"
    else
        read -r -p "$label: " ans </dev/tty
    fi
    WIZARD_ANS[$key]="$ans"
}

wizard::_ask_secret() {
    local key="$1" label="$2" ans ans2
    while :; do
        read -r -s -p "$label: " ans </dev/tty; echo ""
        read -r -s -p "$label (confirm): " ans2 </dev/tty; echo ""
        if [[ "$ans" == "$ans2" && -n "$ans" ]]; then
            WIZARD_ANS[$key]="$ans"
            return 0
        fi
        echo "  values mismatch or empty, try again."
    done
}

wizard::_ask_choice() {
    # $1 = key, $2 = label, $3 = default, rest = options
    local key="$1" label="$2" default="$3"; shift 3
    local options=("$@") i ans
    echo "$label"
    for i in "${!options[@]}"; do
        echo "  $((i+1))) ${options[$i]}"
    done
    while :; do
        read -r -p "Choose [1-${#options[@]}] (default: $default): " ans </dev/tty
        ans="${ans:-$default}"
        if [[ "$ans" =~ ^[0-9]+$ ]] && (( ans >= 1 && ans <= ${#options[@]} )); then
            WIZARD_ANS[$key]="${options[$((ans-1))]}"
            return 0
        fi
        echo "  invalid choice."
    done
}

wizard::run() {
    echo ""
    echo "────────────────────────────────────────────────────────────"
    echo "  gSage AI installer — configuration wizard"
    echo "────────────────────────────────────────────────────────────"
    echo ""

    WIZARD_ANS[gsage_version]="$GSAGE_VERSION"
    WIZARD_ANS[gsage_image_registry]="${GSAGE_IMAGE_REGISTRY:-guardiankey}"
    WIZARD_ANS[gsage_install_dir]="${GSAGE_INSTALL_DIR:-/opt/gsage/current}"
    WIZARD_ANS[gsage_dbs_path]="${GSAGE_DBS_PATH:-/opt/gsage/shared/dbs}"
    WIZARD_ANS[gsage_kb_path]="${GSAGE_KB_PATH:-/opt/gsage/shared/knowledge_base}"
    WIZARD_ANS[gsage_custom_code_path]="${GSAGE_CUSTOM_CODE_PATH:-/opt/gsage/shared/custom_code}"

    echo "── Admin bootstrap ──"
    wizard::_ask       admin_email    "Admin email"                          "admin@example.com"
    wizard::_ask_secret admin_password "Admin password (min 12 chars)"
    wizard::_ask       admin_org_name "Initial organization name"            "SOC"

    echo ""
    echo "── Exposed ports (host) ──"
    echo "Only one port is published publicly: the frontend (web UI + /api proxy)."
    wizard::_ask frontend_port "Web UI / API port"               "8080"
    wizard::_ask postgres_port "Postgres port (127.0.0.1 only)"  "5432"

    echo ""
    echo "── LLM provider (maker) ──"
    wizard::_ask_choice llm_provider "Primary maker provider" 1 \
        ollama openai gemini anthropic deepseek
    local prov="${WIZARD_ANS[llm_provider]}"
    case "$prov" in
        ollama)
            wizard::_ask ollama_maker_model "Ollama maker model (ensure it is pullable)" "qwen2.5:14b"
            ;;
        openai)
            wizard::_ask        openai_maker_model "OpenAI model" "gpt-4o-mini"
            wizard::_ask_secret openai_api_key     "OpenAI API key"
            ;;
        gemini)
            wizard::_ask        gemini_maker_model "Gemini model" "gemini-1.5-pro"
            wizard::_ask_secret gemini_api_key     "Gemini API key"
            ;;
        anthropic)
            wizard::_ask        anthropic_maker_model "Anthropic model" "claude-3-5-sonnet-latest"
            wizard::_ask_secret anthropic_api_key     "Anthropic API key"
            ;;
        deepseek)
            wizard::_ask        deepseek_maker_model "DeepSeek model" "deepseek-chat"
            wizard::_ask_secret deepseek_api_key     "DeepSeek API key"
            ;;
    esac

    echo ""
    echo "Configuration collected. Ready to render /opt/gsage/shared/.env."
}

wizard::_gen() {
    # Cryptographically strong random string suitable for .env (base64, 32 bytes).
    openssl rand -base64 36 | tr -d '=+/' | cut -c1-40
}

wizard::render_env() {
    # $1 = path to env.template, $2 = path to output .env
    local template="$1" out="$2"
    local generated_default generated_curator
    generated_default="$(wizard::_gen)"
    generated_curator="$(wizard::_gen)"

    umask 077
    local tmp
    tmp="$(mktemp)"
    cp "$template" "$tmp"

    # Replace @@GENERATED_CURATOR@@ FIRST (longer match) so it doesn't get eaten by
    # the generic @@GENERATED@@ pass. Use a unique per-line generated value for each
    # remaining @@GENERATED@@ occurrence.
    local safe_curator
    safe_curator="$(printf '%s' "$generated_curator" | sed 's/[\/&]/\\&/g')"
    sed -i "s/@@GENERATED_CURATOR@@/$safe_curator/g" "$tmp"

    # For every remaining @@GENERATED@@, substitute a fresh random value.
    local line new_file
    new_file="$(mktemp)"
    while IFS= read -r line; do
        while [[ "$line" == *"@@GENERATED@@"* ]]; do
            local g safe_g
            g="$(wizard::_gen)"
            safe_g="$(printf '%s' "$g" | sed 's/[\/&]/\\&/g')"
            line="${line/@@GENERATED@@/$g}"
            # loop handles multiple hits on the same line
            : "$safe_g"
        done
        printf '%s\n' "$line" >> "$new_file"
    done < "$tmp"
    mv "$new_file" "$tmp"

    # Resolve @@PROMPT:key@@ and @@PROMPT_OPT:key@@.
    for key in "${!WIZARD_ANS[@]}"; do
        local val safe_val
        val="${WIZARD_ANS[$key]}"
        safe_val="$(printf '%s' "$val" | sed 's/[\/&]/\\&/g')"
        sed -i "s/@@PROMPT:${key}@@/$safe_val/g; s/@@PROMPT_OPT:${key}@@/$safe_val/g" "$tmp"
    done

    # Leftover @@PROMPT_OPT:*@@ → blank (user skipped optional field).
    sed -i 's/@@PROMPT_OPT:[^@]*@@//g' "$tmp"

    # Any leftover @@PROMPT:*@@ is a bug — fail loud.
    if grep -q '@@PROMPT:' "$tmp"; then
        echo "ERROR: unresolved @@PROMPT:*@@ placeholders in .env:" >&2
        grep '@@PROMPT:' "$tmp" >&2
        rm -f "$tmp"
        return 1
    fi

    install -m 0600 "$tmp" "$out"
    rm -f "$tmp"
    echo "Rendered: $out"
}
