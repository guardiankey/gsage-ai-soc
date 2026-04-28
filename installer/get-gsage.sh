#!/usr/bin/env bash
# get-gsage.sh — one-liner bootstrap: downloads the gSage release bundle,
# verifies the SHA-256 checksum, extracts it, and runs the installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/guardiankey/gsage-ai-soc/main/installer/get-gsage.sh | sudo bash
#
# Or with a specific version:
#   curl -fsSL https://raw.githubusercontent.com/guardiankey/gsage-ai-soc/main/installer/get-gsage.sh | sudo bash -s -- --version 0.5.0

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
GSAGE_VERSION="0.6.0"
GSAGE_DIST_BASE="https://github.com/guardiankey/gsage-ai-soc/raw/refs/heads/main/dist"

# Allow overriding the version via --version flag.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            GSAGE_VERSION="$2"; shift 2 ;;
        --version=*)
            GSAGE_VERSION="${1#*=}"; shift ;;
        *)
            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

BUNDLE_FILE="gsage-${GSAGE_VERSION}.tar.gz"
BUNDLE_URL="${GSAGE_DIST_BASE}/${BUNDLE_FILE}"
CHECKSUM_URL="${BUNDLE_URL}.sha256"

# ── Helpers ──────────────────────────────────────────────────────────
_die()  { echo "ERROR: $*" >&2; exit 1; }
_need() { command -v "$1" &>/dev/null || _die "'$1' is required but not installed."; }

_need curl
_need sha256sum
_need tar

# ── Work directory ───────────────────────────────────────────────────
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
cd "$WORK_DIR"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  gSage AI — bootstrap installer"
echo "  version: $GSAGE_VERSION"
echo "════════════════════════════════════════════════════════════"
echo ""

# ── 1. Download bundle + checksum ────────────────────────────────────
echo ">> downloading $BUNDLE_FILE ..."
curl -fL --progress-bar -o "$BUNDLE_FILE" "$BUNDLE_URL" \
    || _die "failed to download bundle from $BUNDLE_URL"

echo ">> downloading checksum ..."
curl -fsSL -o "${BUNDLE_FILE}.sha256" "$CHECKSUM_URL" \
    || _die "failed to download checksum from $CHECKSUM_URL"

# ── 2. Verify SHA-256 ────────────────────────────────────────────────
echo ">> verifying SHA-256 ..."
# The .sha256 file may contain just the hash or a BSD/GNU-style line.
# Normalise: ensure the filename matches what sha256sum expects.
EXPECTED_HASH="$(awk '{print $1}' "${BUNDLE_FILE}.sha256")"
ACTUAL_HASH="$(sha256sum "$BUNDLE_FILE" | awk '{print $1}')"
if [[ "$EXPECTED_HASH" != "$ACTUAL_HASH" ]]; then
    _die "checksum mismatch!
  expected : $EXPECTED_HASH
  actual   : $ACTUAL_HASH
  The downloaded bundle may be corrupted or tampered with."
fi
echo "   SHA-256 OK ($ACTUAL_HASH)"

# ── 3. Extract ───────────────────────────────────────────────────────
echo ">> extracting ..."
tar -xzf "$BUNDLE_FILE"

# Locate the top-level directory produced by the tarball.
BUNDLE_DIR="$(tar -tzf "$BUNDLE_FILE" | head -1 | cut -d/ -f1)"
[[ -d "$BUNDLE_DIR" ]] || _die "could not locate extracted directory '$BUNDLE_DIR'."

# ── 4. Run installer ─────────────────────────────────────────────────
INSTALLER="$WORK_DIR/$BUNDLE_DIR/installer.sh"
[[ -f "$INSTALLER" ]] || _die "installer.sh not found inside the bundle."
chmod +x "$INSTALLER"

echo ">> launching installer ..."
echo ""
exec sudo bash "$INSTALLER"
