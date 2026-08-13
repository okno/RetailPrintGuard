#!/usr/bin/env bash
# Stop the application in a controlled order without stopping MariaDB or nginx.

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

rpg_require_root
rpg_acquire_lock
rpg_stop_control_plane
systemctl stop retailprintguard-pos-proxy.service retailprintguard-rch-proxy.service
systemctl stop retailprintguard.target
rpg_note "RetailPrintGuard application services stopped; MariaDB and nginx were left running."
