#!/usr/bin/env bash
# shellcheck disable=SC2034  # constants are consumed by scripts that source this library
# Shared constants and fail-closed helpers for Debian lifecycle scripts.

set -Eeuo pipefail
IFS=$'\n\t'
umask 027

readonly RPG_ETC_DIR="/etc/retailprintguard"
readonly RPG_CONFIG_PATH="${RPG_ETC_DIR}/config.yaml"
readonly RPG_DATABASE_ENV="${RPG_ETC_DIR}/database.env"
readonly RPG_DATABASE_PASSWORD_FILE="${RPG_ETC_DIR}/database.password"
readonly RPG_JWT_SECRET_FILE="${RPG_ETC_DIR}/jwt.secret"
readonly RPG_APP_ROOT="/opt/retailprintguard"
readonly RPG_RELEASES_DIR="${RPG_APP_ROOT}/releases"
readonly RPG_CURRENT_LINK="${RPG_APP_ROOT}/current"
readonly RPG_DATA_ROOT="/var/lib/retailprintguard"
readonly RPG_SPOOL_ROOT="${RPG_DATA_ROOT}/spool"
readonly RPG_ARCHIVE_ROOT="${RPG_DATA_ROOT}/archive"
readonly RPG_STATE_ROOT="${RPG_DATA_ROOT}/state"
readonly RPG_BACKUP_ROOT="/var/backups/retailprintguard"
readonly RPG_LOG_ROOT="/var/log/retailprintguard"
readonly RPG_WEB_ROOT="/var/www/retailprintguard"
readonly RPG_WEB_RELEASES="${RPG_WEB_ROOT}/releases"
readonly RPG_WEB_CURRENT="${RPG_WEB_ROOT}/current"
readonly RPG_LOCK_FILE="/run/lock/retailprintguard-maintenance.lock"
readonly RPG_DATABASE_NAME="retailprintguard"
readonly RPG_DATABASE_USER="retailprintguard_app"
readonly RPG_SERVICES=(
    retailprintguard-pos-proxy.service
    retailprintguard-rch-proxy.service
    retailprintguard-ingestion.service
    retailprintguard-parser.service
    retailprintguard-correlation.service
    retailprintguard-fraud.service
    retailprintguard-api.service
)

rpg_note() {
    printf '[RetailPrintGuard] %s\n' "$*"
}

rpg_die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

rpg_require_root() {
    [[ "${EUID}" -eq 0 ]] || rpg_die "run this command as root"
}

rpg_require_command() {
    command -v "$1" >/dev/null 2>&1 || rpg_die "required command is missing: $1"
}

rpg_acquire_lock() {
    if [[ "${RPG_MAINTENANCE_LOCK_HELD:-0}" == 1 ]]; then
        return
    fi
    install -d -m 0755 -- /run/lock
    exec 9>"${RPG_LOCK_FILE}"
    flock -n 9 || rpg_die "another RetailPrintGuard maintenance operation is active"
}

rpg_assert_managed_path() {
    local candidate="$1"
    case "${candidate}" in
        "${RPG_ETC_DIR}"|"${RPG_ETC_DIR}/"*|\
        "${RPG_APP_ROOT}"|"${RPG_APP_ROOT}/"*|\
        "${RPG_DATA_ROOT}"|"${RPG_DATA_ROOT}/"*|\
        "${RPG_BACKUP_ROOT}"|"${RPG_BACKUP_ROOT}/"*|\
        "${RPG_LOG_ROOT}"|"${RPG_LOG_ROOT}/"*|\
        "${RPG_WEB_ROOT}"|"${RPG_WEB_ROOT}/"*) ;;
        *) rpg_die "refusing path outside managed roots: ${candidate}" ;;
    esac
}

rpg_atomic_symlink() {
    local target="$1"
    local link="$2"
    local temporary="${link}.new.$$"
    rpg_assert_managed_path "${link}"
    if [[ -e "${link}" && ! -L "${link}" ]]; then
        rpg_die "refusing to replace a non-symlink path: ${link}"
    fi
    ln -s -- "${target}" "${temporary}"
    mv -Tf -- "${temporary}" "${link}"
}

rpg_current_release() {
    if [[ -L "${RPG_CURRENT_LINK}" ]]; then
        readlink -f -- "${RPG_CURRENT_LINK}"
    fi
}

rpg_stop_control_plane() {
    local service
    for service in retailprintguard-api.service retailprintguard-fraud.service \
        retailprintguard-correlation.service retailprintguard-parser.service \
        retailprintguard-ingestion.service; do
        systemctl stop "${service}" 2>/dev/null || true
    done
}

rpg_restart_installed_services() {
    local service
    for service in "${RPG_SERVICES[@]}"; do
        if systemctl is-enabled --quiet "${service}"; then
            systemctl restart "${service}"
        fi
    done
    systemctl reload nginx.service
}

rpg_validate_archive_member() {
    local archive="$1"
    python3 - "${archive}" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1], "r:gz") as bundle:
    for member in bundle.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unsupported archive member type: {member.name}")
PY
}
