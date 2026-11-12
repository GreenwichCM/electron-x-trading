#!/usr/bin/env bash
# What production needs that git does not carry, as one file to copy to the always-on machine over RDP:
#
#   bash deploy/pack-secrets.sh            settings files and the production certificates
#                                          -> ~/trading-prod-secrets.tgz
#   bash deploy/pack-secrets.sh --state    drop copy's sequence numbers and fill journal, for the switch-over;
#                                          stop production on this machine first -> ~/trading-prod-state.tgz
#
# Unpack it in ~/trading on the other machine (see README.md), then delete every copy: the secrets file holds
# passwords and the private key.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
if [ "${1:-}" = "--state" ]; then
    if [ -e var/prod/dc/lock ] && ! flock -n var/prod/dc/lock true; then
        echo "production's dropcopy is still running here: stop it first (Ctrl-C in its honcho)"
        exit 1
    fi
    tar czf ~/trading-prod-state.tgz var/prod/dc
    echo "wrote ~/trading-prod-state.tgz ($(tar tzf ~/trading-prod-state.tgz | wc -l) entries)"
    exit 0
fi
files=(services/prod.env services/db.power.env
    services/marketdata/.env services/marketdata/prod.env services/dropcopy/.env services/dropcopy/prod.env
    services/riskmonitor/.env services/recorder/.env services/api/.env
    certs/prod/ca.crt certs/prod/client.crt certs/prod/client.key)
missing=()
for f in "${files[@]}"; do
    [ -e "$f" ] || missing+=("$f")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "missing: ${missing[*]}"
    exit 1
fi
tar czf ~/trading-prod-secrets.tgz "${files[@]}"
echo "wrote ~/trading-prod-secrets.tgz (${#files[@]} files): copy it over RDP, unpack it, then delete every copy"
