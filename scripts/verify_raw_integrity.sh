#!/usr/bin/env bash
# Read-only verification for backup archives or captured evidence roots.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
archive=""
sidecar=""
replay_args=()

usage() {
    cat <<'EOF'
Usage:
  verify_raw_integrity.sh --archive FILE.tar.gz --sidecar FILE.sha256
  verify_raw_integrity.sh [run_offline_replay.sh evidence options]

The command lists/validates archives but never extracts or modifies originals.
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --archive) (( $# >= 2 )) || { usage >&2; exit 64; }; archive="$2"; shift 2 ;;
        --sidecar) (( $# >= 2 )) || { usage >&2; exit 64; }; sidecar="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) replay_args+=("$1"); shift ;;
    esac
done

if [[ -n "${archive}" || -n "${sidecar}" ]]; then
    [[ -n "${archive}" && -n "${sidecar}" && ${#replay_args[@]} -eq 0 ]] || {
        printf 'ERROR: archive mode requires both --archive and --sidecar only\n' >&2
        exit 64
    }
    [[ -f "${archive}" && ! -L "${archive}" ]] || { printf 'ERROR: unsafe archive\n' >&2; exit 66; }
    [[ -f "${sidecar}" && ! -L "${sidecar}" ]] || { printf 'ERROR: unsafe sidecar\n' >&2; exit 66; }
    command -v gzip >/dev/null
    command -v tar >/dev/null
    command -v sha256sum >/dev/null
    gzip -t -- "${archive}"
    archive_abs="$(cd -- "$(dirname -- "${archive}")" && pwd -P)/$(basename -- "${archive}")"
    sidecar_records="$(awk 'NF {count += 1} END {print count + 0}' "${sidecar}")"
    [[ "${sidecar_records}" == 1 ]] || {
        printf 'ERROR: sidecar must contain exactly one non-empty record\n' >&2
        exit 65
    }
    expected="$(awk 'NF == 2 {print tolower($1)}' "${sidecar}")"
    [[ "${expected}" =~ ^[0-9a-f]{64}$ ]] || {
        printf 'ERROR: sidecar must contain exactly one SHA-256 record\n' >&2
        exit 65
    }
    actual="$(sha256sum -- "${archive_abs}" | awk '{print $1}')"
    [[ "${actual}" == "${expected}" ]] || { printf 'ERROR: archive SHA-256 mismatch\n' >&2; exit 65; }
    member_list="$(mktemp)"
    trap 'rm -f -- "${member_list:-}"' EXIT
    tar -tzf "${archive_abs}" >"${member_list}"
    while IFS= read -r member; do
        [[ -n "${member}" ]] || continue
        [[ "${member}" != /* && "${member}" != .. && "${member}" != ../* && \
           "${member}" != *'/../'* && "${member}" != *'/..' ]] || {
            printf 'ERROR: unsafe archive member: %s\n' "${member}" >&2
            exit 65
        }
    done <"${member_list}"
    rm -f -- "${member_list}"
    trap - EXIT
    printf 'PASS: archive compression, SHA-256 sidecar and member paths verified\n'
    exit 0
fi

(( ${#replay_args[@]} > 0 )) || { usage >&2; exit 64; }
exec "${SCRIPT_DIR}/run_offline_replay.sh" "${replay_args[@]}"
