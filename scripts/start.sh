#!/usr/bin/env bash
# Start all RetailPrintGuard application services. MariaDB/nginx remain distro services.

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

rpg_require_root
rpg_acquire_lock
systemctl reset-failed "${RPG_SERVICES[@]}" retailprintguard.target
systemctl start mariadb.service nginx.service retailprintguard.target
for service in "${RPG_SERVICES[@]}"; do
    systemctl is-active --quiet "${service}" || rpg_die "service failed to start: ${service}"
done
rpg_note "All RetailPrintGuard application services are active."
