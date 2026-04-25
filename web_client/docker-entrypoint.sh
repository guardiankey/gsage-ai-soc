#!/bin/sh
# docker-entrypoint.sh — select the nginx profile based on GSAGE_NGINX_PROFILE.
#
# Profiles:
#   prod (default) — /api + /lists only.
#   dev            — adds /kibana and /wikijs proxies.
set -eu

PROFILE="${GSAGE_NGINX_PROFILE:-prod}"
CONF_DIR="/etc/nginx/conf.d"
SRC_DIR="/etc/nginx/gsage-profiles"

case "$PROFILE" in
    prod) SRC="$SRC_DIR/nginx.prod.conf" ;;
    dev)  SRC="$SRC_DIR/nginx.dev.conf"  ;;
    *)
        echo "ERROR: invalid GSAGE_NGINX_PROFILE='$PROFILE' (expected: dev|prod)" >&2
        exit 1
        ;;
esac

if [ ! -f "$SRC" ]; then
    echo "ERROR: nginx profile file not found: $SRC" >&2
    exit 1
fi

echo "gsage-frontend: using nginx profile '$PROFILE' ($SRC)"
cp "$SRC" "$CONF_DIR/default.conf"

exec nginx -g 'daemon off;'
