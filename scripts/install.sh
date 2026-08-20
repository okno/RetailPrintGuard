#!/usr/bin/env bash
# Idempotent Debian installer. It never adds/removes addresses, routes, DNS or firewall rules.

set -Eeuo pipefail
IFS=$'\n\t'
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

config_source=""
frontend_source="${SOURCE_ROOT}/frontend/dist"
start_services=yes
control_plane_only=no
replace_config=no
allow_unlocked=no

usage() {
    printf '%s\n' \
        'Usage: sudo ./scripts/install.sh [options]' \
        '  --config FILE          required on first install; approved site values only' \
        '  --replace-config       replace an existing installed config with --config' \
        '  --frontend-dir DIR     prebuilt Vite dist (default: frontend/dist)' \
        '  --no-start             install and enable nothing; do not start services' \
        '  --control-plane-only   activate without restarting the two proxy services' \
        '  --allow-unlocked       development only: permit installation without requirements lock' \
        '  --help'
}

while (( $# > 0 )); do
    case "$1" in
        --config)
            (( $# >= 2 )) || rpg_die "--config requires a file"
            config_source="$2"
            shift 2
            ;;
        --replace-config)
            replace_config=yes
            shift
            ;;
        --frontend-dir)
            (( $# >= 2 )) || rpg_die "--frontend-dir requires a directory"
            frontend_source="$2"
            shift 2
            ;;
        --no-start)
            start_services=no
            shift
            ;;
        --control-plane-only)
            control_plane_only=yes
            shift
            ;;
        --allow-unlocked)
            allow_unlocked=yes
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *) rpg_die "unknown argument: $1" ;;
    esac
done

if [[ "${start_services}" == no && "${control_plane_only}" == yes ]]; then
    rpg_die "--no-start and --control-plane-only are mutually exclusive"
fi
if [[ "${control_plane_only}" == yes && ! -L "${RPG_CURRENT_LINK}" ]]; then
    rpg_die "--control-plane-only requires an existing active installation"
fi

rpg_require_root
rpg_acquire_lock
[[ -r /etc/os-release ]] || rpg_die "cannot identify the operating system"
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "debian" ]] || rpg_die "only Debian is supported"
case "${VERSION_ID:-}" in
    12|13) ;;
    *) rpg_die "supported Debian releases are 12 and 13 (found ${VERSION_ID:-unknown})" ;;
esac

if [[ ! -e "${RPG_CONFIG_PATH}" && -z "${config_source}" ]]; then
    rpg_die "--config is required for the first installation"
fi
if [[ -n "${config_source}" ]]; then
    [[ -f "${config_source}" && ! -L "${config_source}" ]] || \
        rpg_die "configuration must be a regular non-symlink file"
    config_source="$(readlink -f -- "${config_source}")"
fi
[[ -d "${frontend_source}" && -f "${frontend_source}/index.html" ]] || \
    rpg_die "prebuilt frontend is missing; expected ${frontend_source}/index.html"
frontend_source="$(readlink -f -- "${frontend_source}")"

pos_proxy_pid_before=""
rch_proxy_pid_before=""
if [[ "${control_plane_only}" == yes ]]; then
    active_release="$(readlink -f -- "${RPG_CURRENT_LINK}")"
    # Fail before package installation, database configuration or Alembic DDL.
    # The checked closure contains the relay, its direct shared modules, runtime
    # dependency lock and both units.  The active processes are then verified
    # again after the control-plane switch.
    rpg_assert_data_plane_unchanged "${active_release}" "${SOURCE_ROOT}"
    systemctl is-active --quiet retailprintguard-pos-proxy.service || \
        rpg_die "POS proxy must be active for --control-plane-only"
    systemctl is-active --quiet retailprintguard-rch-proxy.service || \
        rpg_die "RCH proxy must be active for --control-plane-only"
    pos_proxy_pid_before="$(systemctl show retailprintguard-pos-proxy.service -p MainPID --value)"
    rch_proxy_pid_before="$(systemctl show retailprintguard-rch-proxy.service -p MainPID --value)"
    [[ "${pos_proxy_pid_before}" =~ ^[1-9][0-9]*$ ]] || rpg_die "invalid POS proxy PID"
    [[ "${rch_proxy_pid_before}" =~ ^[1-9][0-9]*$ ]] || rpg_die "invalid RCH proxy PID"
fi

required_packages=(
    ca-certificates curl gzip iproute2 logrotate mariadb-client mariadb-server \
    nginx openssl python3 python3-pip python3-venv rsync tar tesseract-ocr \
    tesseract-ocr-eng tesseract-ocr-ita util-linux
)
if [[ "${control_plane_only}" == yes ]]; then
    rpg_note "Verifying existing Debian dependencies without package changes"
    rpg_require_command dpkg-query
    missing_packages=()
    for package in "${required_packages[@]}"; do
        if ! dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | \
            grep -qx 'install ok installed'; then
            missing_packages+=("${package}")
        fi
    done
    (( ${#missing_packages[@]} == 0 )) || rpg_die \
        "system dependencies changed; use an approved proxy maintenance window: ${missing_packages[*]}"
else
    rpg_note "Installing Debian dependencies"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "${required_packages[@]}"
fi

for command in cmp find flock install mariadb openssl python3 rsync runuser sha256sum systemctl tesseract; do
    rpg_require_command "${command}"
done

rpg_note "Creating least-privilege service identities"
for group in retailprintguard-config retailprintguard-db retailprintguard-spool \
    retailprintguard-pos-proxy retailprintguard-rch-proxy \
    retailprintguard-worker retailprintguard-api; do
    getent group "${group}" >/dev/null || groupadd --system "${group}"
done
for proxy_identity in retailprintguard-pos-proxy retailprintguard-rch-proxy; do
    if ! id "${proxy_identity}" >/dev/null 2>&1; then
        useradd --system --gid "${proxy_identity}" --home-dir /nonexistent \
            --shell /usr/sbin/nologin "${proxy_identity}"
    fi
    usermod -a -G retailprintguard-config,retailprintguard-spool "${proxy_identity}"
done
if ! id retailprintguard-worker >/dev/null 2>&1; then
    useradd --system --gid retailprintguard-worker --home-dir /nonexistent \
        --shell /usr/sbin/nologin retailprintguard-worker
fi
if ! id retailprintguard-api >/dev/null 2>&1; then
    useradd --system --gid retailprintguard-api --home-dir /nonexistent \
        --shell /usr/sbin/nologin retailprintguard-api
fi
usermod -a -G retailprintguard-config,retailprintguard-db,retailprintguard-spool \
    retailprintguard-worker
usermod -a -G retailprintguard-config,retailprintguard-db retailprintguard-api

install -d -m 0750 -o root -g retailprintguard-config -- "${RPG_ETC_DIR}"
install -d -m 0755 -o root -g root -- "${RPG_APP_ROOT}" "${RPG_RELEASES_DIR}"
install -d -m 0750 -o root -g retailprintguard-spool -- "${RPG_SPOOL_ROOT}"
install -d -m 2770 -o retailprintguard-worker -g retailprintguard-spool -- \
    "${RPG_ARCHIVE_ROOT}"
install -d -m 0750 -o retailprintguard-worker -g retailprintguard-worker -- \
    "${RPG_STATE_ROOT}"
install -d -m 0700 -o root -g root -- "${RPG_BACKUP_ROOT}"
install -d -m 0750 -o root -g adm -- "${RPG_LOG_ROOT}"
install -d -m 0750 -o root -g retailprintguard-spool -- \
    "${RPG_LOG_ROOT}/proxy"
install -d -m 0750 -o retailprintguard-worker -g retailprintguard-worker -- \
    "${RPG_LOG_ROOT}/worker"
install -d -m 0755 -o root -g root -- "${RPG_WEB_ROOT}" "${RPG_WEB_RELEASES}"

if [[ -n "${config_source}" && ( ! -e "${RPG_CONFIG_PATH}" || "${replace_config}" == yes ) ]]; then
    install -m 0640 -o root -g retailprintguard-config -- \
        "${config_source}" "${RPG_CONFIG_PATH}"
elif [[ -e "${RPG_CONFIG_PATH}" ]]; then
    [[ -f "${RPG_CONFIG_PATH}" && ! -L "${RPG_CONFIG_PATH}" ]] || \
        rpg_die "installed configuration is not a regular file"
    chown root:retailprintguard-config "${RPG_CONFIG_PATH}"
    chmod 0640 "${RPG_CONFIG_PATH}"
    rpg_note "Preserved existing ${RPG_CONFIG_PATH}"
fi

release_hash="$(python3 - "${SOURCE_ROOT}" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
ignored_dirs = {".git", ".venv", ".pytest_cache", ".ruff_cache", "node_modules", "__pycache__"}
ignored_files = {"alembic_autogen.db"}
digest = hashlib.sha256()
for directory, dirnames, filenames in os.walk(root):
    dirnames[:] = sorted(name for name in dirnames if name not in ignored_dirs)
    base = Path(directory)
    for name in sorted(filenames):
        if name in ignored_files or name.endswith((".pyc", ".pyo")):
            continue
        path = base / name
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
print(digest.hexdigest()[:20])
PY
)"
[[ "${release_hash}" =~ ^[0-9a-f]{20}$ ]] || rpg_die "cannot calculate release identity"
release_path="${RPG_RELEASES_DIR}/${release_hash}"
rpg_assert_managed_path "${release_path}"

if [[ ! -d "${release_path}" ]]; then
    # Virtualenv console scripts contain absolute shebangs and cannot be
    # relocated safely. Build at the final content-addressed path; the global
    # installer lock and .release-complete marker keep it unpublished until
    # construction succeeds, while the trap removes incomplete releases.
    install -d -m 0755 -o root -g root -- "${release_path}"
    cleanup_release() { rm -rf -- "${release_path}"; }
    trap cleanup_release EXIT
    rsync -a \
        --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=.ruff_cache \
        --exclude=node_modules --exclude=__pycache__ --exclude='*.pyc' \
        --exclude=alembic_autogen.db -- "${SOURCE_ROOT}/" "${release_path}/"
    python3 -m venv "${release_path}/.venv"
    if [[ -f "${release_path}/requirements/production.lock" && \
          -f "${release_path}/requirements/build.lock" ]]; then
        "${release_path}/.venv/bin/pip" install --require-hashes \
            -r "${release_path}/requirements/build.lock"
        "${release_path}/.venv/bin/pip" install --require-hashes \
            -r "${release_path}/requirements/production.lock"
        "${release_path}/.venv/bin/pip" install --no-deps --no-build-isolation \
            "${release_path}"
    elif [[ "${allow_unlocked}" == yes ]]; then
        rpg_note "WARNING: installing unpinned dependencies by explicit request"
        "${release_path}/.venv/bin/pip" install "${release_path}"
    else
        rpg_die "requirements/build.lock or production.lock is missing; refusing an unreproducible install"
    fi
    # The installer runs with umask 027, while isolated service identities do
    # not share root's group. Release code contains no secrets and must be
    # readable/traversable (but never writable) by those identities.
    chmod -R a+rX,go-w "${release_path}"
    for entrypoint in alembic retailprintguard-api retailprintguard-correlate \
        retailprintguard-fraud retailprintguard-ingestion retailprintguard-parser \
        retailprintguard-proxy; do
        entrypoint_path="${release_path}/.venv/bin/${entrypoint}"
        [[ -x "${entrypoint_path}" ]] || rpg_die "installed entrypoint is missing: ${entrypoint}"
        IFS= read -r entrypoint_shebang <"${entrypoint_path}"
        case "${entrypoint_shebang}" in
            "#!${release_path}/.venv/bin/python"|"#!${release_path}/.venv/bin/python3") ;;
            *) rpg_die "installed entrypoint has a non-final shebang: ${entrypoint}" ;;
        esac
    done
    chmod 0755 "${release_path}"/scripts/*.sh \
        "${release_path}/scripts/validate_site_config.py"
    for service_identity in retailprintguard-pos-proxy retailprintguard-rch-proxy; do
        runuser -u "${service_identity}" -- \
            "${release_path}/.venv/bin/python" -c \
            'import retailprintguard; assert retailprintguard.__version__' || \
            rpg_die "release is not executable by ${service_identity}"
    done
    touch "${release_path}/.release-complete"
    trap - EXIT
else
    [[ -f "${release_path}/.release-complete" ]] || \
        rpg_die "existing release is incomplete: ${release_path}"
    rpg_note "Release ${release_hash} already installed"
fi

"${release_path}/.venv/bin/python" "${release_path}/scripts/validate_site_config.py" \
    --config "${RPG_CONFIG_PATH}" --require-deployment-layout --require-assigned-listeners

device_listing="$(
    "${release_path}/.venv/bin/python" "${release_path}/scripts/validate_site_config.py" \
        --config "${RPG_CONFIG_PATH}" --require-deployment-layout \
        --list-device-directories
)"
while IFS=$'\t' read -r device_type device_id; do
    case "${device_type}" in
        pos) device_owner=retailprintguard-pos-proxy ;;
        rch) device_owner=retailprintguard-rch-proxy ;;
        *) rpg_die "unsupported validated device type: ${device_type}" ;;
    esac
    [[ "${device_id}" =~ ^[a-z][a-z0-9_-]{1,63}$ ]] || \
        rpg_die "unsafe validated device id: ${device_id}"
    install -d -m 0750 -o "${device_owner}" -g retailprintguard-spool -- \
        "${RPG_SPOOL_ROOT}/${device_id}"
done <<<"${device_listing}"

rpg_note "Configuring MariaDB for loopback-only application access"
if [[ "${control_plane_only}" == yes ]]; then
    [[ -f /etc/mysql/mariadb.conf.d/70-retailprintguard.cnf ]] || \
        rpg_die "installed MariaDB configuration is missing"
    cmp -s -- "${SOURCE_ROOT}/deploy/mariadb/70-retailprintguard.cnf" \
        /etc/mysql/mariadb.conf.d/70-retailprintguard.cnf || rpg_die \
        "MariaDB configuration changed; use an approved proxy maintenance window"
    systemctl is-active --quiet mariadb.service || rpg_die "MariaDB is not active"
else
    install -m 0644 -o root -g root -- "${SOURCE_ROOT}/deploy/mariadb/70-retailprintguard.cnf" \
        /etc/mysql/mariadb.conf.d/70-retailprintguard.cnf
    systemctl enable --now mariadb.service
    systemctl restart mariadb.service
fi

if [[ ! -e "${RPG_DATABASE_PASSWORD_FILE}" ]]; then
    openssl rand -hex 32 >"${RPG_DATABASE_PASSWORD_FILE}"
fi
[[ -f "${RPG_DATABASE_PASSWORD_FILE}" && ! -L "${RPG_DATABASE_PASSWORD_FILE}" ]] || \
    rpg_die "database password file must be a regular non-symlink file"
chmod 0600 "${RPG_DATABASE_PASSWORD_FILE}"
chown root:root "${RPG_DATABASE_PASSWORD_FILE}"
database_password="$(tr -d '\r\n' <"${RPG_DATABASE_PASSWORD_FILE}")"
[[ "${database_password}" =~ ^[0-9a-f]{64}$ ]] || rpg_die "invalid database password file"

mariadb --protocol=socket <<SQL
CREATE DATABASE IF NOT EXISTS ${RPG_DATABASE_NAME}
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${RPG_DATABASE_USER}'@'127.0.0.1'
  IDENTIFIED BY '${database_password}';
ALTER USER '${RPG_DATABASE_USER}'@'127.0.0.1'
  IDENTIFIED BY '${database_password}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${RPG_DATABASE_USER}'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON ${RPG_DATABASE_NAME}.*
  TO '${RPG_DATABASE_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

database_url="mysql+pymysql://${RPG_DATABASE_USER}:${database_password}@127.0.0.1:3306/${RPG_DATABASE_NAME}?charset=utf8mb4"
database_env_tmp="$(mktemp "${RPG_ETC_DIR}/.database.env.XXXXXXXX")"
printf 'RPG_DATABASE_URL=%s\n' "${database_url}" >"${database_env_tmp}"
chmod 0640 "${database_env_tmp}"
chown root:retailprintguard-db "${database_env_tmp}"
mv -f -- "${database_env_tmp}" "${RPG_DATABASE_ENV}"

if [[ -L "${RPG_CURRENT_LINK}" && -x "${RPG_CURRENT_LINK}/scripts/backup.sh" ]]; then
    rpg_note "Creating a pre-migration backup of the installed release"
    RPG_MAINTENANCE_LOCK_HELD=1 "${RPG_CURRENT_LINK}/scripts/backup.sh" >/dev/null
fi

if [[ ! -e "${RPG_JWT_SECRET_FILE}" ]]; then
    openssl rand -hex 48 >"${RPG_JWT_SECRET_FILE}"
fi
[[ -f "${RPG_JWT_SECRET_FILE}" && ! -L "${RPG_JWT_SECRET_FILE}" ]] || \
    rpg_die "JWT secret must be a regular non-symlink file"
chmod 0640 "${RPG_JWT_SECRET_FILE}"
chown root:retailprintguard-api "${RPG_JWT_SECRET_FILE}"
if [[ ! -e "${RPG_ETC_DIR}/ingestion.env" ]]; then
    install -m 0640 -o root -g retailprintguard-config -- \
        "${SOURCE_ROOT}/deploy/ingestion.env.example" "${RPG_ETC_DIR}/ingestion.env"
fi
if [[ ! -e "${RPG_ETC_DIR}/parser.env" ]]; then
    install -m 0640 -o root -g retailprintguard-config -- \
        "${SOURCE_ROOT}/deploy/parser.env.example" "${RPG_ETC_DIR}/parser.env"
fi

rpg_note "Applying versioned database migrations with an ephemeral DDL account"
migration_password="$(openssl rand -hex 32)"
cleanup_migration_user() {
    mariadb --protocol=socket -e \
        "DROP USER IF EXISTS 'retailprintguard_migrate'@'127.0.0.1'; FLUSH PRIVILEGES;" \
        >/dev/null 2>&1 || true
}
trap cleanup_migration_user EXIT
mariadb --protocol=socket <<SQL
CREATE USER 'retailprintguard_migrate'@'127.0.0.1'
  IDENTIFIED BY '${migration_password}';
GRANT ALL PRIVILEGES ON ${RPG_DATABASE_NAME}.*
  TO 'retailprintguard_migrate'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
RPG_DATABASE_URL="mysql+pymysql://retailprintguard_migrate:${migration_password}@127.0.0.1:3306/${RPG_DATABASE_NAME}?charset=utf8mb4" \
    "${release_path}/.venv/bin/python" -m alembic \
        -c "${release_path}/alembic.ini" upgrade head
cleanup_migration_user
trap - EXIT

frontend_hash="$(python3 - "${frontend_source}" <<'PY'
import hashlib
import os
import sys
from pathlib import Path
root = Path(sys.argv[1])
digest = hashlib.sha256()
for directory, _, filenames in os.walk(root):
    for name in sorted(filenames):
        path = Path(directory) / name
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(path.read_bytes())
print(digest.hexdigest()[:20])
PY
)"
web_release="${RPG_WEB_RELEASES}/${frontend_hash}"
rpg_assert_managed_path "${web_release}"

normalize_web_tree() {
    local web_tree="$1"
    local unexpected_entry=""

    [[ -d "${web_tree}" && ! -L "${web_tree}" ]] || \
        rpg_die "frontend release must be a regular directory: ${web_tree}"
    unexpected_entry="$(
        find "${web_tree}" -xdev ! \( -type d -o -type f \) -print -quit
    )"
    [[ -z "${unexpected_entry}" ]] || \
        rpg_die "frontend release contains a symlink or special file: ${unexpected_entry}"

    # mktemp creates the staging root as 0700 and the installer uses umask 027.
    # Nginx runs as www-data, so make the static, non-secret web tree explicitly
    # traversable/readable. Apply this to existing content-addressed releases as
    # well: a rebuilt release may legitimately have the same frontend hash.
    chown -R root:root -- "${web_tree}"
    find "${web_tree}" -xdev -type d -exec chmod 0755 -- {} +
    find "${web_tree}" -xdev -type f -exec chmod 0644 -- {} +

    [[ -f "${web_tree}/index.html" && ! -L "${web_tree}/index.html" ]] || \
        rpg_die "frontend release has no regular index.html: ${web_tree}"
    if ! runuser -u www-data -- test -x "${web_tree}" || \
       ! runuser -u www-data -- test -r "${web_tree}/index.html"; then
        rpg_die "frontend release is not readable by nginx: ${web_tree}"
    fi
}

if [[ ! -d "${web_release}" ]]; then
    web_stage="$(mktemp -d "${RPG_WEB_RELEASES}/.stage.XXXXXXXX")"
    rsync -a -- "${frontend_source}/" "${web_stage}/"
    normalize_web_tree "${web_stage}"
    mv -- "${web_stage}" "${web_release}"
fi
# Repair and verify an already content-addressed release too. In particular,
# this fixes releases created by older installers that preserved staging 0700.
normalize_web_tree "${web_release}"

previous_release="$(rpg_current_release)"
if [[ -n "${previous_release}" && "${previous_release}" != "${release_path}" ]]; then
    printf '%s\n' "${previous_release}" >"${RPG_STATE_ROOT}/previous-release"
    chmod 0640 "${RPG_STATE_ROOT}/previous-release"
fi
if [[ -L "${RPG_WEB_CURRENT}" ]]; then
    readlink -f -- "${RPG_WEB_CURRENT}" >"${RPG_STATE_ROOT}/previous-web-release"
    chmod 0640 "${RPG_STATE_ROOT}/previous-web-release"
fi
printf '%s\n' "${web_release}" >"${RPG_STATE_ROOT}/release-web-${release_hash}"
chmod 0640 "${RPG_STATE_ROOT}/release-web-${release_hash}"
if [[ "${start_services}" == no ]]; then
    # --no-start is also a no-activation mode.  Existing services keep using
    # their current code and a crash cannot unexpectedly select the staged
    # release; the operator can activate later with update/install normally.
    rpg_note "Release ${release_hash} and frontend ${frontend_hash} staged but not activated"
    printf '%s\n' "${release_path}" >"${RPG_STATE_ROOT}/staged-release"
    printf '%s\n' "${web_release}" >"${RPG_STATE_ROOT}/staged-web-release"
    chmod 0640 "${RPG_STATE_ROOT}/staged-release" "${RPG_STATE_ROOT}/staged-web-release"
    rpg_note "Services and current symlinks were left unchanged (--no-start)"
    exit 0
fi

rpg_atomic_symlink "${release_path}" "${RPG_CURRENT_LINK}"
rpg_atomic_symlink "${web_release}" "${RPG_WEB_CURRENT}"

rpg_note "Installing hardened systemd, nginx and logrotate definitions"
for unit in "${SOURCE_ROOT}"/systemd/*; do
    unit_name="$(basename -- "${unit}")"
    installed_unit="/etc/systemd/system/${unit_name}"
    if [[ "${control_plane_only}" == yes && "${unit_name}" =~ ^retailprintguard-(pos|rch)-proxy\.service$ ]]; then
        [[ -f "${installed_unit}" && ! -L "${installed_unit}" ]] || \
            rpg_die "installed proxy unit is missing or unsafe: ${installed_unit}"
        cmp -s -- "${unit}" "${installed_unit}" || \
            rpg_die "installed proxy unit differs during control-plane update: ${unit_name}"
        rpg_note "Preserved installed proxy unit without rewriting it: ${unit_name}"
        continue
    fi
    install -m 0644 -o root -g root -- "${unit}" "${installed_unit}"
done
install -m 0644 -o root -g root -- "${SOURCE_ROOT}/deploy/nginx/retailprintguard.conf" \
    /etc/nginx/sites-available/retailprintguard.conf
ln -sfn -- /etc/nginx/sites-available/retailprintguard.conf \
    /etc/nginx/sites-enabled/retailprintguard.conf
install -m 0644 -o root -g root -- "${SOURCE_ROOT}/deploy/logrotate/retailprintguard" \
    /etc/logrotate.d/retailprintguard

systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/retailprintguard*.service \
    /etc/systemd/system/retailprintguard*.timer \
    /etc/systemd/system/retailprintguard.target
nginx -t

systemctl enable retailprintguard.target
systemctl enable --now retailprintguard-backup.timer
systemctl enable nginx.service
if [[ "${control_plane_only}" != yes ]]; then
    systemctl restart retailprintguard-pos-proxy.service retailprintguard-rch-proxy.service
fi
systemctl restart retailprintguard-ingestion.service retailprintguard-parser.service \
    retailprintguard-correlation.service \
    retailprintguard-fraud.service retailprintguard-api.service
systemctl reload nginx.service

if [[ "${control_plane_only}" == yes ]]; then
    [[ "$(systemctl show retailprintguard-pos-proxy.service -p MainPID --value)" == \
        "${pos_proxy_pid_before}" ]] || rpg_die "POS proxy PID changed during control-plane update"
    [[ "$(systemctl show retailprintguard-rch-proxy.service -p MainPID --value)" == \
        "${rch_proxy_pid_before}" ]] || rpg_die "RCH proxy PID changed during control-plane update"
    rpg_note "Proxy PIDs were preserved; no listener process was restarted"
fi

rpg_note "Installed release ${release_hash}"
rpg_note "No address, route, DNS or firewall setting was changed."
rpg_note "UI listener: http://<server-ip>:8081/ (0.0.0.0:8081; restrict with firewall)"
