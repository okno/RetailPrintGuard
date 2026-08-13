#!/usr/bin/env bash
# Atomically select a previously installed application release; database data is untouched.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

release=""
while (( $# > 0 )); do
    case "$1" in
        --release)
            (( $# >= 2 )) || rpg_die "--release requires an installed release name"
            release="$2"
            shift 2
            ;;
        --help|-h)
            printf 'Usage: sudo %s [--release 20_HEX_CHARS]\n' "$0"
            exit 0
            ;;
        *) rpg_die "unknown argument: $1" ;;
    esac
done

rpg_require_root
rpg_acquire_lock
if [[ -z "${release}" ]]; then
    [[ -f "${RPG_STATE_ROOT}/previous-release" ]] || rpg_die "no previous release recorded"
    release="$(basename -- "$(<"${RPG_STATE_ROOT}/previous-release")")"
fi
[[ "${release}" =~ ^[0-9a-f]{20}$ ]] || rpg_die "invalid release identity"
target="${RPG_RELEASES_DIR}/${release}"
rpg_assert_managed_path "${target}"
[[ -d "${target}" && -f "${target}/.release-complete" ]] || \
    rpg_die "release is missing or incomplete: ${target}"
current="$(rpg_current_release)"
[[ "${current}" != "${target}" ]] || rpg_die "release ${release} is already active"

"${target}/.venv/bin/python" "${target}/scripts/validate_site_config.py" \
    --config "${RPG_CONFIG_PATH}" --require-deployment-layout --require-assigned-listeners
printf '%s\n' "${current}" >"${RPG_STATE_ROOT}/previous-release"
web_mapping="${RPG_STATE_ROOT}/release-web-${release}"
[[ -f "${web_mapping}" && ! -L "${web_mapping}" ]] || \
    rpg_die "no frontend mapping is recorded for application release ${release}"
web_target="$(<"${web_mapping}")"
rpg_assert_managed_path "${web_target}"
[[ -d "${web_target}" && -f "${web_target}/index.html" ]] || \
    rpg_die "previous frontend release is missing or incomplete: ${web_target}"
current_web="$(readlink -f -- "${RPG_WEB_CURRENT}")"
printf '%s\n' "${current_web}" >"${RPG_STATE_ROOT}/previous-web-release"
switched_app=no
switched_web=no
restore_links() {
    local status=$?
    if [[ "${switched_web}" == yes ]]; then
        rpg_atomic_symlink "${current_web}" "${RPG_WEB_CURRENT}" || true
    fi
    if [[ "${switched_app}" == yes ]]; then
        rpg_atomic_symlink "${current}" "${RPG_CURRENT_LINK}" || true
    fi
    if [[ "${switched_app}" == yes || "${switched_web}" == yes ]]; then
        rpg_restart_installed_services || true
    fi
    exit "${status}"
}
trap restore_links ERR
rpg_atomic_symlink "${target}" "${RPG_CURRENT_LINK}"
switched_app=yes
rpg_atomic_symlink "${web_target}" "${RPG_WEB_CURRENT}"
switched_web=yes
rpg_restart_installed_services
trap - ERR
rpg_note "Rolled back application and matching frontend to ${release}; database was not downgraded"
rpg_note "Use restore.sh with an explicit backup only when a database rollback is required"
