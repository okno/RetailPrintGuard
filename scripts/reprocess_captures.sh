#!/usr/bin/env bash
# Controlled append-only parser/correlation/fraud reprocessing. Dry-run by default.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

config="${RPG_CONFIG:-/etc/retailprintguard/config.yaml}"
limit=1000
execute=no
reason=""
parser_name=""
parser_version=""
build_sha256=""

usage() {
    cat <<'EOF'
Usage: sudo reprocess_captures.sh [options]
  --config FILE
  --limit N
  --reason TEXT
  --activate-parser NAME --parser-version VERSION --build-sha256 HEX64
  --execute

Default is dry-run. Execute requires root, inactive parser/correlation/fraud
workers, a reason and a verified backup. POS/RCH proxies are never stopped.
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --config) (( $# >= 2 )) || { usage >&2; exit 64; }; config="$2"; shift 2 ;;
        --limit) (( $# >= 2 )) || { usage >&2; exit 64; }; limit="$2"; shift 2 ;;
        --reason) (( $# >= 2 )) || { usage >&2; exit 64; }; reason="$2"; shift 2 ;;
        --activate-parser) (( $# >= 2 )) || { usage >&2; exit 64; }; parser_name="$2"; shift 2 ;;
        --parser-version) (( $# >= 2 )) || { usage >&2; exit 64; }; parser_version="$2"; shift 2 ;;
        --build-sha256) (( $# >= 2 )) || { usage >&2; exit 64; }; build_sha256="$2"; shift 2 ;;
        --execute) execute=yes; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 64 ;;
    esac
done

if [[ ! "${limit}" =~ ^[0-9]+$ ]] || (( limit < 1 || limit > 10000 )); then
    printf 'ERROR: --limit must be between 1 and 10000\n' >&2
    exit 64
fi
[[ -f "${config}" && ! -L "${config}" ]] || { printf 'ERROR: unsafe config\n' >&2; exit 66; }
activation_count=0
[[ -z "${parser_name}" ]] || (( activation_count += 1 ))
[[ -z "${parser_version}" ]] || (( activation_count += 1 ))
[[ -z "${build_sha256}" ]] || (( activation_count += 1 ))
(( activation_count == 0 || activation_count == 3 )) || {
    printf 'ERROR: parser activation requires NAME, VERSION and HEX64 together\n' >&2
    exit 64
}
[[ -z "${build_sha256}" || "${build_sha256}" =~ ^[0-9a-fA-F]{64}$ ]] || {
    printf 'ERROR: invalid parser build SHA-256\n' >&2
    exit 64
}

release_root="$(readlink -f -- /opt/retailprintguard/current 2>/dev/null || true)"
[[ -n "${release_root}" && -d "${release_root}" ]] || {
    printf 'ERROR: installed release not found\n' >&2
    exit 69
}
parser_bin="${release_root}/.venv/bin/retailprintguard-parser"
correlation_bin="${release_root}/.venv/bin/retailprintguard-correlate"
fraud_bin="${release_root}/.venv/bin/retailprintguard-fraud"
for executable in "${parser_bin}" "${correlation_bin}" "${fraud_bin}"; do
    [[ -x "${executable}" ]] || { printf 'ERROR: missing entrypoint: %s\n' "${executable}" >&2; exit 69; }
done

parser_cmd=("${parser_bin}" --config "${config}" --once --reparse-all --limit "${limit}" --json)
correlation_cmd=("${correlation_bin}" --config "${config}" --once --max-documents "${limit}" --json)
if (( activation_count == 3 )); then
    correlation_cmd+=(--activate-parser "${parser_name}" --parser-version "${parser_version}" \
        --build-sha256 "${build_sha256}" --activation-reason "${reason:-dry-run preview}")
fi
fraud_cmd=("${fraud_bin}" --config "${config}" --once --max-transactions "${limit}" --json)

printf 'Mode: %s\n' "$([[ "${execute}" == yes ]] && printf execute || printf dry-run)"
printf 'Parser command:'; printf ' %q' "${parser_cmd[@]}"; printf '\n'
printf 'Correlation command:'; printf ' %q' "${correlation_cmd[@]}"; printf '\n'
printf 'Fraud command:'; printf ' %q' "${fraud_cmd[@]}"; printf '\n'
[[ "${execute}" == yes ]] || {
    printf 'DRY RUN: no database or evidence was modified\n'
    exit 0
}

(( EUID == 0 )) || { printf 'ERROR: --execute requires root\n' >&2; exit 77; }
(( ${#reason} >= 8 )) || { printf 'ERROR: --execute requires a meaningful --reason\n' >&2; exit 64; }
(( activation_count == 3 )) || {
    printf 'ERROR: --execute requires an exact parser activation identity for auditability\n' >&2
    exit 64
}
command -v systemctl >/dev/null || { printf 'ERROR: systemctl not found\n' >&2; exit 69; }
for service in retailprintguard-parser.service retailprintguard-correlation.service retailprintguard-fraud.service; do
    if systemctl is-active --quiet "${service}"; then
        printf 'ERROR: stop only control-plane worker %s before reprocessing\n' "${service}" >&2
        exit 75
    fi
done
database_env="/etc/retailprintguard/database.env"
[[ -f "${database_env}" && ! -L "${database_env}" ]] || { printf 'ERROR: unsafe database.env\n' >&2; exit 66; }
[[ "$(stat -c '%U' "${database_env}")" == root && \
   "$(stat -c '%a' "${database_env}")" =~ ^(400|440|600|640)$ ]] || {
    printf 'ERROR: database.env ownership or mode is unsafe\n' >&2
    exit 77
}
# database.env is generated root-owned by the installer and contains one quoted-safe URL.
set -a
# shellcheck disable=SC1090
source "${database_env}"
set +a
for command in gzip openssl sha256sum tar; do
    command -v "${command}" >/dev/null || { printf 'ERROR: missing command: %s\n' "${command}" >&2; exit 69; }
done
backup_path="/var/backups/retailprintguard/reprocess-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 4).tar.gz"
"${release_root}/scripts/backup.sh" --output "${backup_path}"
[[ -f "${backup_path}" && ! -L "${backup_path}" ]] || { printf 'ERROR: backup was not created safely\n' >&2; exit 65; }
gzip -t -- "${backup_path}"
tar -tzf "${backup_path}" >/dev/null
backup_dir="$(cd -- "$(dirname -- "${backup_path}")" && pwd -P)"
backup_name="$(basename -- "${backup_path}")"
(cd -- "${backup_dir}" && sha256sum -- "${backup_name}" >"${backup_name}.sha256" && \
    sha256sum -c -- "${backup_name}.sha256" >/dev/null)
chmod 0600 "${backup_path}.sha256"
printf 'Reprocessing reason: %s\n' "${reason}"
"${parser_cmd[@]}"
"${correlation_cmd[@]}"
"${fraud_cmd[@]}"
unset RPG_DATABASE_URL
printf 'PASS: append-only reprocessing completed; workers remain stopped for review\n'
