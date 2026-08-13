#!/usr/bin/env bash
# Explicit disaster-recovery restore. Proxies continue relaying while the control plane is stopped.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

archive=""
confirmed=no
while (( $# > 0 )); do
    case "$1" in
        --archive)
            (( $# >= 2 )) || rpg_die "--archive requires a file"
            archive="$2"
            shift 2
            ;;
        --confirm-destructive-database-restore)
            confirmed=yes
            shift
            ;;
        --help|-h)
            printf '%s\n' \
                "Usage: sudo $0 --archive FILE.tar.gz --confirm-destructive-database-restore" \
                'The current database is backed up, dropped and restored. Ready evidence is merged.'
            exit 0
            ;;
        *) rpg_die "unknown argument: $1" ;;
    esac
done

rpg_require_root
[[ "${confirmed}" == yes ]] || rpg_die "explicit destructive restore confirmation is required"
[[ -f "${archive}" && ! -L "${archive}" ]] || rpg_die "backup must be a regular file"
archive="$(readlink -f -- "${archive}")"
rpg_acquire_lock
rpg_validate_archive_member "${archive}"

rpg_note "Creating a safety backup before restore"
RPG_MAINTENANCE_LOCK_HELD=1 "${SCRIPT_DIR}/backup.sh" >/dev/null

stage="$(mktemp -d "${RPG_BACKUP_ROOT}/.restore.XXXXXXXX")"
cleanup() { rm -rf -- "${stage}"; }
trap cleanup EXIT
tar -C "${stage}" -xzf "${archive}"
(
    cd -- "${stage}"
    [[ -f MANIFEST.sha256 ]] || rpg_die "backup manifest is missing"
    sha256sum -c MANIFEST.sha256 >/dev/null
)
[[ -f "${stage}/database/retailprintguard.sql.gz" ]] || \
    rpg_die "backup does not contain the MariaDB dump"

rpg_stop_control_plane
rpg_note "Replacing only the RetailPrintGuard database"
mariadb --protocol=socket <<SQL
DROP DATABASE IF EXISTS ${RPG_DATABASE_NAME};
CREATE DATABASE ${RPG_DATABASE_NAME}
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
SQL
gzip -dc -- "${stage}/database/retailprintguard.sql.gz" | mariadb --protocol=socket

merge_evidence_fail_closed() {
    local source="$1"
    local destination="$2"
    local copied_owner="$3"
    local relative existing
    [[ -d "${source}" ]] || return 0
    local candidate_list
    candidate_list="$(mktemp "${RPG_BACKUP_ROOT}/.restore-list.XXXXXXXX")"
    if ! find "${source}" -type f -print0 >"${candidate_list}"; then
        rm -f -- "${candidate_list}"
        rpg_die "cannot enumerate restored evidence: ${source}"
    fi
    while IFS= read -r -d '' candidate; do
        relative="${candidate#"${source}/"}"
        [[ "${relative}" != "${candidate}" && "${relative}" != *".."* ]] || \
            rpg_die "unsafe restored evidence path: ${candidate}"
        existing="${destination}/${relative}"
        if [[ -e "${existing}" ]]; then
            [[ -f "${existing}" && ! -L "${existing}" ]] || \
                rpg_die "restore collision is not a regular file: ${existing}"
            cmp -s -- "${candidate}" "${existing}" || \
                rpg_die "restore collision differs from backup: ${existing}"
        fi
    done <"${candidate_list}"
    rm -f -- "${candidate_list}"
    # Backup member ownership is not trusted on another host. New entries get
    # a known local identity; existing evidence is never overwritten.
    rsync -a --ignore-existing --chown="${copied_owner}" -- "${source}/" "${destination}/"
}
merge_evidence_fail_closed \
    "${stage}/evidence/spool" "${RPG_SPOOL_ROOT}" "root:retailprintguard-spool"
merge_evidence_fail_closed \
    "${stage}/evidence/archive" "${RPG_ARCHIVE_ROOT}" \
    "retailprintguard-worker:retailprintguard-spool"

# Restore the per-device ownership contract used by the two isolated proxy
# identities. There intentionally is no shared `retailprintguard-proxy` user.
chown root:retailprintguard-spool "${RPG_SPOOL_ROOT}"
chmod 0750 "${RPG_SPOOL_ROOT}"
device_listing="$(
    "${RPG_CURRENT_LINK}/.venv/bin/python" \
        "${RPG_CURRENT_LINK}/scripts/validate_site_config.py" \
        --config "${RPG_CONFIG_PATH}" --require-deployment-layout \
        --list-device-directories
)"
while read -r device_type device_id; do
    case "${device_type}" in
        pos) device_owner=retailprintguard-pos-proxy ;;
        rch) device_owner=retailprintguard-rch-proxy ;;
        *) rpg_die "unsupported validated device type during restore: ${device_type}" ;;
    esac
    [[ "${device_id}" =~ ^[a-z][a-z0-9_-]{1,63}$ ]] || \
        rpg_die "unsafe validated device id during restore: ${device_id}"
    device_root="${RPG_SPOOL_ROOT}/${device_id}"
    if [[ -e "${device_root}" || -L "${device_root}" ]]; then
        [[ -d "${device_root}" && ! -L "${device_root}" ]] || \
            rpg_die "device spool root is not a regular directory: ${device_root}"
    else
        install -d -m 0750 -o "${device_owner}" -g retailprintguard-spool -- \
            "${device_root}"
    fi
    chown -R -- "${device_owner}:retailprintguard-spool" "${device_root}"
    chmod 0750 "${device_root}"
done <<<"${device_listing}"
chown retailprintguard-worker:retailprintguard-spool "${RPG_ARCHIVE_ROOT}"
chmod 2770 "${RPG_ARCHIVE_ROOT}"

[[ -x "${RPG_CURRENT_LINK}/.venv/bin/alembic" ]] || rpg_die "installed Alembic is missing"
# The restored application account remains local and retains its database-level grant.
database_url="$(awk -F= '$1 == "RPG_DATABASE_URL" {print substr($0, index($0, "=") + 1)}' \
    "${RPG_DATABASE_ENV}")"
[[ -n "${database_url}" ]] || rpg_die "RPG_DATABASE_URL is missing from database.env"
export RPG_DATABASE_URL="${database_url}"
"${RPG_CURRENT_LINK}/.venv/bin/alembic" -c "${RPG_CURRENT_LINK}/alembic.ini" upgrade head
unset RPG_DATABASE_URL

for service in retailprintguard-ingestion.service retailprintguard-parser.service \
    retailprintguard-correlation.service retailprintguard-fraud.service \
    retailprintguard-api.service; do
    if systemctl is-enabled --quiet "${service}"; then
        systemctl restart "${service}"
    fi
done
rpg_note "Restore completed; proxy services were not stopped"
