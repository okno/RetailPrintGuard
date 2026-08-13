#!/usr/bin/env bash
# Online-consistent DB backup plus immutable ready evidence. Active *.partial jobs are excluded.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

output=""
while (( $# > 0 )); do
    case "$1" in
        --output)
            (( $# >= 2 )) || rpg_die "--output requires a file"
            output="$2"
            shift 2
            ;;
        --help|-h)
            printf 'Usage: sudo %s [--output FILE.tar.gz]\n' "$0"
            exit 0
            ;;
        *) rpg_die "unknown argument: $1" ;;
    esac
done

rpg_require_root
rpg_acquire_lock
for command in gzip mariadb-dump openssl python3 rsync sha256sum tar; do
    rpg_require_command "${command}"
done
install -d -m 0700 -o root -g root -- "${RPG_BACKUP_ROOT}"

if [[ -z "${output}" ]]; then
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    output="${RPG_BACKUP_ROOT}/retailprintguard-${timestamp}-$(openssl rand -hex 4).tar.gz"
fi
output_parent="$(dirname -- "${output}")"
install -d -m 0700 -o root -g root -- "${output_parent}"
output="$(readlink -m -- "${output}")"
[[ ! -e "${output}" ]] || rpg_die "backup destination already exists: ${output}"

stage="$(mktemp -d "${RPG_BACKUP_ROOT}/.backup.XXXXXXXX")"
cleanup() { rm -rf -- "${stage}"; }
trap cleanup EXIT
install -d -m 0700 -- "${stage}/database" "${stage}/configuration" \
    "${stage}/evidence/spool" "${stage}/evidence/archive" "${stage}/deployment"

rpg_note "Dumping MariaDB with a single transaction"
mariadb-dump --protocol=socket --single-transaction --quick --routines --events --triggers \
    --databases "${RPG_DATABASE_NAME}" | gzip -9 >"${stage}/database/retailprintguard.sql.gz"

for name in config.yaml database.env database.password jwt.secret ingestion.env; do
    if [[ -f "${RPG_ETC_DIR}/${name}" && ! -L "${RPG_ETC_DIR}/${name}" ]]; then
        install -m 0600 -- "${RPG_ETC_DIR}/${name}" "${stage}/configuration/${name}"
    fi
done

if [[ -d "${RPG_SPOOL_ROOT}" ]]; then
    marker_list="$(mktemp "${RPG_BACKUP_ROOT}/.ready-list.XXXXXXXX")"
    if ! find "${RPG_SPOOL_ROOT}" -type f -name .ready -print0 >"${marker_list}"; then
        rm -f -- "${marker_list}"
        rpg_die "cannot enumerate ready evidence for backup"
    fi
    while IFS= read -r -d '' marker; do
        job_dir="$(dirname -- "${marker}")"
        relative="${job_dir#"${RPG_SPOOL_ROOT}/"}"
        [[ "${relative}" != "${job_dir}" && "${relative}" != *".."* ]] || \
            rpg_die "unsafe ready-job path discovered: ${job_dir}"
        install -d -m 0700 -- "${stage}/evidence/spool/$(dirname -- "${relative}")"
        rsync -a -- "${job_dir}/" "${stage}/evidence/spool/${relative}/"
    done <"${marker_list}"
    rm -f -- "${marker_list}"
fi
if [[ -d "${RPG_ARCHIVE_ROOT}" ]]; then
    rsync -a --exclude='*.partial' -- "${RPG_ARCHIVE_ROOT}/" \
        "${stage}/evidence/archive/"
fi

if [[ -L "${RPG_CURRENT_LINK}" ]]; then
    readlink -f -- "${RPG_CURRENT_LINK}" >"${stage}/deployment/current-release"
fi
if [[ -L "${RPG_WEB_CURRENT}" ]]; then
    readlink -f -- "${RPG_WEB_CURRENT}" >"${stage}/deployment/current-web-release"
fi
printf 'created_at_utc=%s\nformat=retailprintguard-backup-v1\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${stage}/BACKUP-INFO"
(
    cd -- "${stage}"
    manifest_tmp="$(mktemp /tmp/retailprintguard-manifest.XXXXXXXX)"
    find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | \
        xargs -0 sha256sum >"${manifest_tmp}"
    mv -- "${manifest_tmp}" MANIFEST.sha256
    sha256sum -c MANIFEST.sha256 >/dev/null
)
tar -C "${stage}" -czf "${output}" .
chmod 0600 "${output}"
rpg_note "Backup created: ${output}"
printf '%s\n' "${output}"
