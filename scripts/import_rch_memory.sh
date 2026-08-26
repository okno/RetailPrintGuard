#!/usr/bin/env bash
# Validate or import immutable RCH DGFE XML exports. This script never contacts a device.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

config="${RPG_CONFIG:-/etc/retailprintguard/config.yaml}"
database_env="/etc/retailprintguard/database.env"
inbox=""
device_id=""
instance_id="rch-xtools-primary"
max_jobs=10000
execute=no
reason=""
output=""
output_tmp=""

# shellcheck disable=SC2317  # Invoked indirectly by the EXIT trap below.
cleanup() {
    [[ -z "${output_tmp}" ]] || rm -f -- "${output_tmp}"
    unset RPG_DATABASE_URL RPG_MAINTENANCE_LOCK_HELD
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage: import_rch_memory.sh --inbox DIR --device DEVICE_ID [options]
  --config FILE           RetailPrintGuard site configuration
  --instance ID           Stable source instance (default: rch-xtools-primary)
  --max-jobs N            Maximum XML candidates (default: 10000)
  --output FILE           Atomically publish the JSON report
  --reason TEXT           Required audit reason with --execute
  --execute               Persist through the trusted DB repository

Default is read-only validation. The inbox must already contain complete XML
exports produced by an authorized RCH tool. This script never opens a device
socket and never stops, starts or restarts proxy services.
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --config) (( $# >= 2 )) || { usage >&2; exit 64; }; config="$2"; shift 2 ;;
        --inbox) (( $# >= 2 )) || { usage >&2; exit 64; }; inbox="$2"; shift 2 ;;
        --device) (( $# >= 2 )) || { usage >&2; exit 64; }; device_id="$2"; shift 2 ;;
        --instance) (( $# >= 2 )) || { usage >&2; exit 64; }; instance_id="$2"; shift 2 ;;
        --max-jobs) (( $# >= 2 )) || { usage >&2; exit 64; }; max_jobs="$2"; shift 2 ;;
        --output) (( $# >= 2 )) || { usage >&2; exit 64; }; output="$2"; shift 2 ;;
        --reason) (( $# >= 2 )) || { usage >&2; exit 64; }; reason="$2"; shift 2 ;;
        --execute) execute=yes; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 64 ;;
    esac
done

[[ -n "${inbox}" && "${inbox}" == /* ]] || {
    printf 'ERROR: --inbox must be an absolute directory path\n' >&2
    exit 64
}
[[ "${device_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
    printf 'ERROR: --device must be a valid configured device ID\n' >&2
    exit 64
}
[[ "${instance_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
    printf 'ERROR: --instance must use 1-128 safe identifier characters\n' >&2
    exit 64
}
if [[ ! "${max_jobs}" =~ ^[0-9]+$ ]] || (( max_jobs < 1 || max_jobs > 100000 )); then
    printf 'ERROR: --max-jobs must be between 1 and 100000\n' >&2
    exit 64
fi
[[ -f "${config}" && ! -L "${config}" ]] || {
    printf 'ERROR: config must be a regular non-symlink file: %s\n' "${config}" >&2
    exit 66
}
[[ -d "${inbox}" && ! -L "${inbox}" ]] || {
    printf 'ERROR: inbox must be a directory and not a symlink: %s\n' "${inbox}" >&2
    exit 66
}
command -v find >/dev/null || { printf 'ERROR: find not found\n' >&2; exit 69; }
command -v stat >/dev/null || { printf 'ERROR: stat not found\n' >&2; exit 69; }
inbox_mode="$(stat -c '%a' -- "${inbox}")"
[[ "${inbox_mode}" =~ ^[0-7]{3,4}$ ]] || {
    printf 'ERROR: cannot determine inbox permissions\n' >&2
    exit 77
}
if (( (8#${inbox_mode} & 2) != 0 )); then
    printf 'ERROR: inbox must not be world-writable\n' >&2
    exit 77
fi
if [[ -n "$(find "${inbox}" -xdev -type l -print -quit)" ]]; then
    printf 'ERROR: inbox contains a symlink; refusing ambiguous evidence\n' >&2
    exit 66
fi

if [[ -x "${REPO_ROOT}/.venv/bin/retailprintguard-import" ]]; then
    importer="${REPO_ROOT}/.venv/bin/retailprintguard-import"
elif [[ -x "/opt/retailprintguard/current/.venv/bin/retailprintguard-import" ]]; then
    importer="/opt/retailprintguard/current/.venv/bin/retailprintguard-import"
else
    importer="$(command -v retailprintguard-import || true)"
fi
[[ -n "${importer}" ]] || {
    printf 'ERROR: retailprintguard-import not found\n' >&2
    exit 69
}

args=(
    --config "${config}"
    --dgfe-root "${inbox}"
    --dgfe-device "${device_id}"
    --dgfe-instance "${instance_id}"
    --max-jobs "${max_jobs}"
    --once
    --json
)

if [[ -n "${output}" ]]; then
    output_parent="$(dirname -- "${output}")"
    [[ -d "${output_parent}" && ! -L "${output_parent}" ]] || {
        printf 'ERROR: output parent must be a directory and not a symlink\n' >&2
        exit 66
    }
    [[ ! -e "${output}" && ! -L "${output}" ]] || {
        printf 'ERROR: output already exists: %s\n' "${output}" >&2
        exit 73
    }
    output_tmp="$(mktemp "${output_parent}/.rch-memory-report.XXXXXXXX")"
fi

if [[ "${execute}" == no ]]; then
    args+=(--validate-only)
    printf 'Mode: validate-only (database and source files remain unchanged)\n' >&2
else
    (( EUID == 0 )) || {
        printf 'ERROR: --execute requires root\n' >&2
        exit 77
    }
    (( ${#reason} >= 8 )) || {
        printf 'ERROR: --execute requires a meaningful --reason\n' >&2
        exit 64
    }
    [[ -f "${database_env}" && ! -L "${database_env}" ]] || {
        printf 'ERROR: database.env must be a regular non-symlink file\n' >&2
        exit 66
    }
    database_owner="$(stat -c '%U' -- "${database_env}")"
    database_mode="$(stat -c '%a' -- "${database_env}")"
    [[ "${database_owner}" == root && "${database_mode}" =~ ^(400|440|600|640)$ ]] || {
        printf 'ERROR: database.env ownership or mode is unsafe\n' >&2
        exit 77
    }

    # database.env is generated by install.sh, root-owned and contains a quoted-safe URL.
    set -a
    # shellcheck disable=SC1090
    source "${database_env}"
    set +a
    [[ -n "${RPG_DATABASE_URL:-}" ]] || {
        printf 'ERROR: RPG_DATABASE_URL is missing from database.env\n' >&2
        exit 78
    }

    # Hold the same lock across backup and import; neither operation controls proxies.
    # shellcheck source=scripts/lib.sh
    source "${SCRIPT_DIR}/lib.sh"
    umask 077
    rpg_acquire_lock
    export RPG_MAINTENANCE_LOCK_HELD=1
    printf 'Import reason: %s\n' "${reason}" >&2
    printf 'Creating a verified pre-import backup; proxy services remain untouched\n' >&2
    "${SCRIPT_DIR}/backup.sh" >&2
    args+=(--repository-factory retailprintguard.db.repository:create_ingestion_repository)
fi

if [[ -n "${output}" ]]; then
    set +e
    "${importer}" "${args[@]}" | tee "${output_tmp}"
    pipeline_codes=("${PIPESTATUS[@]}")
    set -e
    status="${pipeline_codes[0]}"
    if (( pipeline_codes[1] != 0 && status == 0 )); then
        status="${pipeline_codes[1]}"
    fi
    chmod 0600 "${output_tmp}"
    mv -- "${output_tmp}" "${output}"
    output_tmp=""
else
    if "${importer}" "${args[@]}"; then
        status=0
    else
        status="$?"
    fi
fi

unset RPG_DATABASE_URL RPG_MAINTENANCE_LOCK_HELD
if (( status == 0 )); then
    printf 'PASS: RCH memory %s completed; no device or proxy socket was opened\n' \
        "$([[ "${execute}" == yes ]] && printf import || printf validation)" >&2
elif (( status == 1 )); then
    printf 'WARNING: import report requires review (quarantine, retry or busy source)\n' >&2
else
    printf 'ERROR: RCH memory importer failed with exit code %s\n' "${status}" >&2
fi
exit "${status}"
