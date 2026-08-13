#!/usr/bin/env bash
# Non-destructive default: disable execution while preserving evidence, DB, config and releases.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

remove_code=no
while (( $# > 0 )); do
    case "$1" in
        --remove-code)
            remove_code=yes
            shift
            ;;
        --help|-h)
            printf '%s\n' \
                "Usage: sudo $0 [--remove-code]" \
                'Data, configuration, secrets, database and backups are always preserved.'
            exit 0
            ;;
        *) rpg_die "unknown argument: $1" ;;
    esac
done

rpg_require_root
rpg_acquire_lock
systemctl disable --now retailprintguard.target 2>/dev/null || true
systemctl disable --now retailprintguard-backup.timer 2>/dev/null || true
for service in "${RPG_SERVICES[@]}"; do
    systemctl disable --now "${service}" 2>/dev/null || true
    rm -f -- "/etc/systemd/system/${service}"
done
rm -f -- /etc/systemd/system/retailprintguard-backup.service \
    /etc/systemd/system/retailprintguard-backup.timer \
    /etc/systemd/system/retailprintguard.target
if [[ -L /etc/nginx/sites-enabled/retailprintguard.conf ]]; then
    rm -f -- /etc/nginx/sites-enabled/retailprintguard.conf
fi
rm -f -- /etc/nginx/sites-available/retailprintguard.conf \
    /etc/logrotate.d/retailprintguard
systemctl daemon-reload
systemctl reload nginx.service 2>/dev/null || true

if [[ "${remove_code}" == yes ]]; then
    rpg_assert_managed_path "${RPG_APP_ROOT}"
    rpg_assert_managed_path "${RPG_WEB_ROOT}"
    rm -rf -- "${RPG_APP_ROOT}" "${RPG_WEB_ROOT}"
fi
rpg_note "Uninstalled service integration"
rpg_note "Preserved ${RPG_ETC_DIR}, ${RPG_DATA_ROOT}, ${RPG_BACKUP_ROOT} and MariaDB"
