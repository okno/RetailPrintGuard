#!/usr/bin/env bash
# Backup first, then reuse the idempotent installer for a new source release.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

frontend_dir=""
if [[ -d "${SCRIPT_DIR}/../frontend/dist" ]]; then
    frontend_dir="$(cd -- "${SCRIPT_DIR}/../frontend/dist" && pwd -P)"
fi
no_start=no
allow_unlocked=no
while (( $# > 0 )); do
    case "$1" in
        --frontend-dir)
            (( $# >= 2 )) || rpg_die "--frontend-dir requires a directory"
            frontend_dir="$2"
            shift 2
            ;;
        --no-start)
            no_start=yes
            shift
            ;;
        --allow-unlocked)
            allow_unlocked=yes
            shift
            ;;
        --help|-h)
            printf 'Usage: sudo %s [--frontend-dir DIR] [--no-start] [--allow-unlocked]\n' "$0"
            exit 0
            ;;
        *) rpg_die "unknown argument: $1" ;;
    esac
done

rpg_require_root
[[ -f "${RPG_CONFIG_PATH}" ]] || rpg_die "RetailPrintGuard is not installed"
"${SCRIPT_DIR}/backup.sh"
args=(--frontend-dir "${frontend_dir}")
[[ "${no_start}" == yes ]] && args+=(--no-start)
[[ "${allow_unlocked}" == yes ]] && args+=(--allow-unlocked)
exec "${SCRIPT_DIR}/install.sh" "${args[@]}"
