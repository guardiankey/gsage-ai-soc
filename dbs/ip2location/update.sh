#!/usr/bin/env bash
# dbs/ip2location/update.sh — Download / update IP2Location databases.
#
# Usage:
#   IP2LOCATION_TOKEN=<your_token> bash dbs/ip2location/update.sh
#
# Required env:
#   IP2LOCATION_TOKEN  — API token from https://www.ip2location.com/development-libraries
#
# Downloads:
#   IP2LOCATION-LITE-DB11.BIN   (IP + country + region + city + lat/lon + zip + time zone)
#   IP2LOCATION-LITE-ASN.BIN    (IP + AS number + AS name)
#
# Lite (free) editions are used by default.  Replace the PRODUCT_CODES with
# commercial codes if you have a paid subscription.
#
# Run this script manually whenever you want to refresh the databases.
# Recommended: monthly cron job (IP2Location updates monthly).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="${SCRIPT_DIR}"
IP2LOCATION_BASE_URL="https://www.ip2location.com/download/"

# Free Lite products — replace with paid product codes if applicable.
declare -A PRODUCTS=(
    ["IP2LOCATION-LITE-DB11.BIN"]="DB11LITEBIN"
    #["IP2LOCATION-LITE-ASN.BIN"]="ASNLITEBIN"
)

if [[ -z "${IP2LOCATION_TOKEN:-}" ]]; then
    echo "ERROR: IP2LOCATION_TOKEN environment variable is not set."
    echo "  Get a free token at: https://www.ip2location.com/development-libraries"
    exit 1
fi

echo "Updating IP2Location databases in: ${DB_DIR}"

for filename in "${!PRODUCTS[@]}"; do
    code="${PRODUCTS[$filename]}"
    url="${IP2LOCATION_BASE_URL}?token=${IP2LOCATION_TOKEN}&file=${code}"
    archive="${DB_DIR}/${filename}.zip"
    dest="${DB_DIR}/${filename}"

    echo "  Downloading ${filename} (${code})..."
    curl -fsSL --retry 3 -o "${archive}" "${url}"

    echo "  Extracting ${filename}..."
    unzip -oq "${archive}" -d "${DB_DIR}"
    rm -f "${archive}"

    if [[ ! -f "${dest}" ]]; then
        echo "  WARNING: expected file ${dest} not found after extraction."
        echo "  Check that product code '${code}' is valid for your account."
    else
        echo "  ${filename} updated."
    fi
done

echo "Done. IP2Location databases are up to date."
