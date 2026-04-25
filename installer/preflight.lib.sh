#!/usr/bin/env bash
# preflight.lib.sh — host compatibility checks for the gSage installer.
#
# All functions print human-readable messages and return non-zero on failure.
# Sourced by installer.sh; not meant to run standalone.

preflight::detect_os() {
    if [[ ! -r /etc/os-release ]]; then
        echo "ERROR: cannot read /etc/os-release — unsupported OS." >&2
        return 1
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    GSAGE_OS_ID="${ID:-unknown}"
    GSAGE_OS_LIKE="${ID_LIKE:-}"
    case "$GSAGE_OS_ID" in
        ubuntu|debian) GSAGE_OS_FAMILY="debian" ;;
        rhel|rocky|almalinux|centos|fedora) GSAGE_OS_FAMILY="rhel" ;;
        *)
            if [[ "$GSAGE_OS_LIKE" == *debian* ]]; then
                GSAGE_OS_FAMILY="debian"
            elif [[ "$GSAGE_OS_LIKE" == *rhel* || "$GSAGE_OS_LIKE" == *fedora* ]]; then
                GSAGE_OS_FAMILY="rhel"
            else
                echo "WARN: unrecognised OS '$GSAGE_OS_ID' — proceeding with debian-family assumptions."
                GSAGE_OS_FAMILY="debian"
            fi
            ;;
    esac
    export GSAGE_OS_ID GSAGE_OS_LIKE GSAGE_OS_FAMILY
    echo "OS family: $GSAGE_OS_FAMILY ($GSAGE_OS_ID)"
}

preflight::check_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64|aarch64|arm64) echo "Architecture: $arch" ;;
        *)
            echo "ERROR: unsupported architecture: $arch" >&2
            return 1
            ;;
    esac
}

preflight::check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: the installer must be run as root (try: sudo bash installer.sh)" >&2
        return 1
    fi
}

preflight::check_disk() {
    local path="$1" min_gb="$2" avail
    avail="$(df -BG --output=avail "$path" 2>/dev/null | tail -1 | tr -dc '0-9')"
    if [[ -z "$avail" ]]; then
        echo "WARN: could not inspect free space at $path — continuing."
        return 0
    fi
    if (( avail < min_gb )); then
        echo "ERROR: need at least ${min_gb}G free at $path (found ${avail}G)" >&2
        return 1
    fi
    echo "Disk free at $path: ${avail}G (ok)"
}

preflight::check_ram() {
    local min_gb="$1" warn_gb="$2" total_kb total_gb
    total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    total_gb=$(( total_kb / 1024 / 1024 ))
    if (( total_gb < min_gb )); then
        echo "ERROR: need at least ${min_gb}G RAM (found ${total_gb}G)" >&2
        return 1
    fi
    if (( total_gb < warn_gb )); then
        echo "WARN: only ${total_gb}G RAM — recommended ${warn_gb}G for a responsive stack."
    else
        echo "RAM: ${total_gb}G (ok)"
    fi
}

preflight::check_tools() {
    local missing=()
    for t in curl tar gzip sha256sum sed awk grep; do
        command -v "$t" >/dev/null 2>&1 || missing+=("$t")
    done
    if (( ${#missing[@]} > 0 )); then
        echo "Missing baseline tools: ${missing[*]}. The installer will install them."
        preflight::_install_packages "${missing[@]}"
    fi
}

preflight::check_ports() {
    local blocked=()
    local ports=("$@")
    for p in "${ports[@]}"; do
        if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -Eq ":${p}$"; then
            blocked+=("$p")
        fi
    done
    if (( ${#blocked[@]} > 0 )); then
        echo "ERROR: the following ports are already in use: ${blocked[*]}" >&2
        echo "       Free them or re-run with alternate ports via the wizard." >&2
        return 1
    fi
    echo "Ports free: ${ports[*]}"
}

preflight::_install_packages() {
    local pkgs=("$@")
    case "$GSAGE_OS_FAMILY" in
        debian)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            apt-get install -y --no-install-recommends "${pkgs[@]}"
            ;;
        rhel)
            if command -v dnf >/dev/null 2>&1; then
                dnf install -y "${pkgs[@]}"
            else
                yum install -y "${pkgs[@]}"
            fi
            ;;
        *)
            echo "ERROR: don't know how to install packages on $GSAGE_OS_FAMILY" >&2
            return 1
            ;;
    esac
}

preflight::ensure_docker() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        echo "Docker: $(docker --version) (ok)"
    else
        echo "Docker not found — installing via get.docker.com..."
        curl -fsSL https://get.docker.com | sh
        systemctl enable --now docker || true
    fi
    if ! docker compose version >/dev/null 2>&1; then
        echo "ERROR: 'docker compose' plugin missing. Install docker-compose-plugin and retry." >&2
        return 1
    fi
    echo "Compose: $(docker compose version | head -1) (ok)"
}

preflight::ensure_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 not found — installing..."
        preflight::_install_packages python3 python3-venv python3-pip
    fi

    # On Debian/Ubuntu, a working venv requires both `venv` and `ensurepip`,
    # the latter shipped only by the version-specific package
    # python3.<minor>-venv (e.g. python3.13-venv on Ubuntu 24.10+).
    # `import venv` may succeed even when ensurepip is missing, so we test
    # ensurepip explicitly and install the versioned package when needed.
    local need_venv_pkg=0
    python3 -c 'import venv'      >/dev/null 2>&1 || need_venv_pkg=1
    python3 -c 'import ensurepip' >/dev/null 2>&1 || need_venv_pkg=1

    if (( need_venv_pkg )); then
        case "$GSAGE_OS_FAMILY" in
            debian)
                local pyver pkg
                pyver="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
                pkg="python3.${pyver#*.}-venv"
                echo "Installing $pkg (and python3-venv as fallback)..."
                # Try the versioned package first; fall back to the generic one.
                preflight::_install_packages "$pkg" || true
                preflight::_install_packages python3-venv
                ;;
            *)
                preflight::_install_packages python3-venv
                ;;
        esac
    fi

    # Final smoke test: actually create a throwaway venv to be sure.
    local probe
    probe="$(mktemp -d)"
    if ! python3 -m venv "$probe/v" >/dev/null 2>&1; then
        rm -rf "$probe"
        echo "ERROR: python3 -m venv is still not functional. Install the" >&2
        echo "       distro's python3-venv (or python3.<minor>-venv) package" >&2
        echo "       manually and re-run the installer." >&2
        return 1
    fi
    rm -rf "$probe"

    echo "Python: $(python3 --version) (ok, venv functional)"
}
