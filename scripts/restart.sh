#!/usr/bin/env bash
# Refuse a restart that would interrupt active printer sessions unless explicitly forced.

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

force=no
case "${1:-}" in
    "") ;;
    --force-active-sessions) force=yes ;;
    *) rpg_die "usage: $0 [--force-active-sessions]" ;;
esac

rpg_require_root
rpg_acquire_lock
if [[ "${force}" != yes ]]; then
    for service in retailprintguard-pos-proxy.service retailprintguard-rch-proxy.service; do
        pid="$(systemctl show --property MainPID --value "${service}" 2>/dev/null || true)"
        if [[ "${pid}" =~ ^[1-9][0-9]*$ ]] && \
           ss -Htnp state established 2>/dev/null | grep -Fq "pid=${pid},"; then
            rpg_die "active printer session belongs to ${service}; retry when idle"
        fi
    done
fi
rpg_stop_control_plane
systemctl restart retailprintguard-pos-proxy.service retailprintguard-rch-proxy.service
systemctl start retailprintguard-ingestion.service retailprintguard-parser.service \
    retailprintguard-correlation.service retailprintguard-fraud.service \
    retailprintguard-api.service
for service in "${RPG_SERVICES[@]}"; do
    systemctl is-active --quiet "${service}" || rpg_die "service failed after restart: ${service}"
done
rpg_note "RetailPrintGuard restarted successfully."
