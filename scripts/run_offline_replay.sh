#!/usr/bin/env bash
# Validate captured evidence through ingestion adapters. This script never opens device sockets.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
config="${RPG_CONFIG:-${REPO_ROOT}/config/retailprintguard.example.yaml}"
canonical_root=""
rch_root=""
rch_parsed_root=""
printproxy_root=""
hmac_key_file=""
max_jobs=10000
output=""
allow_unauthenticated=no

usage() {
    cat <<'EOF'
Usage: run_offline_replay.sh [options]
  --config FILE
  --canonical-root DIR
  --rch-root DIR
  --rch-parsed-root DIR
  --printproxy-root DIR [--hmac-key-file FILE]
  --allow-unauthenticated-printproxy
  --max-jobs N
  --output FILE

At least one evidence root is required. Validation is read-only (`--validate-only`).
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --config) (( $# >= 2 )) || { usage >&2; exit 64; }; config="$2"; shift 2 ;;
        --canonical-root) (( $# >= 2 )) || { usage >&2; exit 64; }; canonical_root="$2"; shift 2 ;;
        --rch-root) (( $# >= 2 )) || { usage >&2; exit 64; }; rch_root="$2"; shift 2 ;;
        --rch-parsed-root) (( $# >= 2 )) || { usage >&2; exit 64; }; rch_parsed_root="$2"; shift 2 ;;
        --printproxy-root) (( $# >= 2 )) || { usage >&2; exit 64; }; printproxy_root="$2"; shift 2 ;;
        --hmac-key-file) (( $# >= 2 )) || { usage >&2; exit 64; }; hmac_key_file="$2"; shift 2 ;;
        --allow-unauthenticated-printproxy) allow_unauthenticated=yes; shift ;;
        --max-jobs) (( $# >= 2 )) || { usage >&2; exit 64; }; max_jobs="$2"; shift 2 ;;
        --output) (( $# >= 2 )) || { usage >&2; exit 64; }; output="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 64 ;;
    esac
done

[[ -f "${config}" && ! -L "${config}" ]] || {
    printf 'ERROR: config must be a regular non-symlink file: %s\n' "${config}" >&2
    exit 66
}
if [[ ! "${max_jobs}" =~ ^[0-9]+$ ]] || (( max_jobs < 1 || max_jobs > 100000 )); then
    printf 'ERROR: --max-jobs must be between 1 and 100000\n' >&2
    exit 64
fi
[[ -n "${canonical_root}${rch_root}${rch_parsed_root}${printproxy_root}" ]] || {
    printf 'ERROR: at least one evidence root is required\n' >&2
    exit 64
}

for evidence_root in "${canonical_root}" "${rch_root}" "${rch_parsed_root}" "${printproxy_root}"; do
    [[ -z "${evidence_root}" || -d "${evidence_root}" ]] || {
        printf 'ERROR: evidence root is not a directory: %s\n' "${evidence_root}" >&2
        exit 66
    }
done
if [[ -n "${hmac_key_file}" ]]; then
    [[ -f "${hmac_key_file}" && ! -L "${hmac_key_file}" ]] || {
        printf 'ERROR: HMAC key must be a regular non-symlink file\n' >&2
        exit 66
    }
fi

if [[ -x "${REPO_ROOT}/.venv/bin/retailprintguard-import" ]]; then
    importer="${REPO_ROOT}/.venv/bin/retailprintguard-import"
elif [[ -x "/opt/retailprintguard/current/.venv/bin/retailprintguard-import" ]]; then
    importer="/opt/retailprintguard/current/.venv/bin/retailprintguard-import"
else
    importer="$(command -v retailprintguard-import || true)"
fi
[[ -n "${importer}" ]] || { printf 'ERROR: retailprintguard-import not found\n' >&2; exit 69; }

args=(--config "${config}" --validate-only --once --json --max-jobs "${max_jobs}")
[[ -z "${canonical_root}" ]] || args+=(--canonical-root "${canonical_root}")
[[ -z "${rch_root}" ]] || args+=(--rch-root "${rch_root}")
[[ -z "${rch_parsed_root}" ]] || args+=(--rch-parsed-root "${rch_parsed_root}")
[[ -z "${printproxy_root}" ]] || args+=(--printproxy-root "${printproxy_root}")
[[ -z "${hmac_key_file}" ]] || args+=(--printproxy-hmac-key-file "${hmac_key_file}")
[[ "${allow_unauthenticated}" == no ]] || args+=(--allow-unauthenticated-printproxy)

if [[ -n "${output}" ]]; then
    output_parent="$(dirname -- "${output}")"
    [[ -d "${output_parent}" ]] || { printf 'ERROR: output parent does not exist\n' >&2; exit 66; }
    [[ ! -e "${output}" ]] || { printf 'ERROR: output already exists\n' >&2; exit 73; }
    output_tmp="$(mktemp "${output_parent}/.offline-replay.XXXXXXXX")"
    trap 'rm -f -- "${output_tmp:-}"' EXIT
    "${importer}" "${args[@]}" | tee "${output_tmp}"
    chmod 0600 "${output_tmp}"
    mv -f -- "${output_tmp}" "${output}"
    trap - EXIT
else
    "${importer}" "${args[@]}"
fi

printf 'PASS: offline validation completed; no device socket was opened\n' >&2
