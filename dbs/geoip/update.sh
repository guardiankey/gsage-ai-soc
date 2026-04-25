#!/usr/bin/env bash
# dbs/geoip/update.sh — Download / update MaxMind GeoLite2 databases.
#
# Usage:
#   MAXMIND_LICENSE_KEY=<your_key> bash dbs/geoip/update.sh
#
# Required env:
#   MAXMIND_LICENSE_KEY  — Free key from https://www.maxmind.com/en/geolite2/signup
#
# Downloads:
#   GeoLite2-City.mmdb
#   GeoLite2-Country.mmdb
#   GeoLite2-ASN.mmdb
#
# Run this script manually whenever you want to refresh the databases.
# Recommended: monthly cron job (MaxMind updates weekly).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="${SCRIPT_DIR}"
#MAXMIND_EDITION_IDS=("GeoLite2-City" "GeoLite2-Country" "GeoLite2-ASN")
MAXMIND_EDITION_IDS=("GeoLite2-ASN")
MAXMIND_BASE_URL="https://download.maxmind.com/app/geoip_download"

if [[ -z "${MAXMIND_LICENSE_KEY:-}" ]]; then
    echo "ERROR: MAXMIND_LICENSE_KEY environment variable is not set."
    echo "  Get a free key at: https://www.maxmind.com/en/geolite2/signup"
    exit 1
fi

echo "Updating MaxMind GeoLite2 databases in: ${DB_DIR}"

for edition in "${MAXMIND_EDITION_IDS[@]}"; do
    url="${MAXMIND_BASE_URL}?edition_id=${edition}&license_key=${MAXMIND_LICENSE_KEY}&suffix=tar.gz"
    archive="${DB_DIR}/${edition}.tar.gz"

    echo "  Downloading ${edition}..."
    curl -fsSL --retry 3 -o "${archive}" "${url}"

    echo "  Extracting ${edition}.mmdb..."
    tar -xzf "${archive}" -C "${DB_DIR}" --strip-components=1 --wildcards "*.mmdb"
    rm -f "${archive}"

    echo "  ${edition}.mmdb updated."
done

echo "Done. GeoLite2 databases are up to date."
