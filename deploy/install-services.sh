#!/usr/bin/env bash
# Production as Linux services, on the always-on machine: one systemd service per process in Procfile.prod
# (except the development web server: the api serves the built web app), grouped as trading.target. systemd
# starts them when WSL starts and restarts any that stops.
#
#   sudo bash deploy/install-services.sh          install or update the services (does not start them)
#   bash deploy/install-services.sh --print       show the service files without installing anything
#
# Run it again after Procfile.prod changes (deploy/update.sh does). Starting: sudo systemctl start trading.target
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
RUN_AS=${SUDO_USER:-$(id -un)}
HOME_DIR=$(getent passwd "$RUN_AS" | cut -d: -f6)
UV=${UV:-$HOME_DIR/.local/bin/uv}
PRINT=0
[ "${1:-}" = "--print" ] && PRINT=1

if [ ! -x "$UV" ]; then
    echo "uv is not at $UV: install it as $RUN_AS (see README.md), or set UV=/path/to/uv"
    exit 1
fi
if [ "$PRINT" = 0 ] && [ "$(id -u)" != 0 ]; then
    echo "run it with sudo: sudo bash deploy/install-services.sh"
    exit 1
fi

target() {
    cat <<UNIT
[Unit]
Description=ElectronX production, record-only (Procfile.prod)
Wants=docker.service network-online.target
After=docker.service network-online.target

[Install]
WantedBy=multi-user.target
UNIT
}

service() { # $1 the name in Procfile.prod, $2 its command
    local cmd=${2#uv run }
    local extra=""
    if [ "$1" = api ]; then
        extra="Environment=API_WEB_DIST=web/dist"
    fi
    cat <<UNIT
[Unit]
Description=ElectronX production: $1
PartOf=trading.target
After=docker.service network-online.target
StartLimitIntervalSec=600
StartLimitBurst=20

[Service]
User=$RUN_AS
WorkingDirectory=$ROOT
ExecStart=$UV run --no-sync $cmd
$extra
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=trading.target
UNIT
}

emit() { # $1 the file name; the content comes on stdin
    if [ "$PRINT" = 1 ]; then
        echo "# ---- /etc/systemd/system/$1"
        cat
    else
        cat >"/etc/systemd/system/$1"
    fi
}

names=()
target | emit trading.target
while IFS= read -r line; do
    case "$line" in
    '' | '#'*) continue ;;
    esac
    name=${line%%:*}
    cmd=$(echo "${line#*:}" | sed 's/^ *//')
    if [ "$name" = web ]; then
        continue # the api serves the built web app instead
    fi
    case "$cmd" in
    "uv run "*) ;;
    *)
        echo "Procfile.prod: $name does not start with 'uv run', so it is not installed"
        continue
        ;;
    esac
    service "$name" "$cmd" | emit "trading-$name.service"
    names+=("trading-$name.service")
done <"$ROOT/Procfile.prod"

if [ "$PRINT" = 1 ]; then
    exit 0
fi
systemctl daemon-reload
systemctl enable trading.target "${names[@]}" >/dev/null 2>&1
echo "installed and enabled: trading.target ${names[*]}"
missing=()
for f in services/prod.env services/db.power.env services/marketdata/prod.env services/dropcopy/prod.env \
    services/api/.env certs/prod/client.key web/dist/index.html; do
    [ -e "$ROOT/$f" ] || missing+=("$f")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "not there yet (see README.md): ${missing[*]}"
fi
echo "start them with: sudo systemctl start trading.target   (only once production is stopped on any other machine)"
