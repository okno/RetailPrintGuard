#!/usr/bin/env bash
# Atomically select a previously installed application release; database data is untouched.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

release=""
control_plane_only=no
while (( $# > 0 )); do
    case "$1" in
        --release)
            (( $# >= 2 )) || rpg_die "--release requires an installed release name"
            release="$2"
            shift 2
            ;;
        --control-plane-only)
            control_plane_only=yes
            shift
            ;;
        --help|-h)
            printf 'Usage: sudo %s [--release 20_HEX_CHARS] [--control-plane-only]\n' "$0"
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
web_mapping="${RPG_STATE_ROOT}/release-web-${release}"
[[ -f "${web_mapping}" && ! -L "${web_mapping}" ]] || \
    rpg_die "no frontend mapping is recorded for application release ${release}"
web_target="$(<"${web_mapping}")"
rpg_assert_managed_path "${web_target}"
[[ -d "${web_target}" && -f "${web_target}/index.html" ]] || \
    rpg_die "previous frontend release is missing or incomplete: ${web_target}"
current_web="$(readlink -f -- "${RPG_WEB_CURRENT}")"

proxy_snapshot() {
    local service pid
    for service in retailprintguard-pos-proxy.service \
        retailprintguard-rch-proxy.service retailprintguard-epson-proxy.service; do
        systemctl show "${service}" -p Id -p ActiveState -p MainPID \
            -p InvocationID -p ExecMainStartTimestampMonotonic --no-pager || return
        sha256sum -- "/etc/systemd/system/${service}" || return
        pid="$(systemctl show "${service}" -p MainPID --value)" || return
        [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || rpg_die "invalid proxy PID: ${service}"
        ss -Hltpn | awk -v needle="pid=${pid}," \
            'index($0, needle) { print $4 }' | LC_ALL=C sort -u || return
    done
    sha256sum -- "${RPG_CONFIG_PATH}"
}

restart_control_plane() {
    local service
    for service in retailprintguard-ingestion.service retailprintguard-parser.service \
        retailprintguard-correlation.service retailprintguard-fraud.service \
        retailprintguard-api.service retailprintguard-export.service \
        retailprintguard-analysis.service; do
        if systemctl is-active --quiet "${service}" || \
            systemctl is-enabled --quiet "${service}"; then
            systemctl restart "${service}"
        fi
    done
    systemctl reload nginx.service
}

proxy_before=""
if [[ "${control_plane_only}" == yes ]]; then
    # Fail before changing a symlink if any executable proxy dependency differs.
    rpg_assert_data_plane_unchanged "${current}" "${target}"
    for service in retailprintguard-pos-proxy.service \
        retailprintguard-rch-proxy.service retailprintguard-epson-proxy.service; do
        systemctl is-active --quiet "${service}" || \
            rpg_die "proxy must remain active for control-plane rollback: ${service}"
        cmp -s -- "${target}/systemd/${service}" "/etc/systemd/system/${service}" || \
            rpg_die "installed proxy unit differs from target release: ${service}"
    done
    proxy_before="$(proxy_snapshot)"
    [[ -n "${proxy_before}" ]] || rpg_die "cannot capture proxy identity"
fi

previous_release_file="${RPG_STATE_ROOT}/previous-release"
previous_web_file="${RPG_STATE_ROOT}/previous-web-release"
previous_release_before=""
previous_web_before=""
had_previous_release=no
had_previous_web=no
if [[ -f "${previous_release_file}" ]]; then
    previous_release_before="$(<"${previous_release_file}")"
    had_previous_release=yes
fi
if [[ -f "${previous_web_file}" ]]; then
    previous_web_before="$(<"${previous_web_file}")"
    had_previous_web=yes
fi
previous_release_changed=no
previous_web_changed=no

switched_app=no
switched_web=no
restore_links() {
    local status=$?
    rm -f -- "${previous_release_file}.new.$$" "${previous_web_file}.new.$$"
    if [[ "${previous_release_changed}" == yes ]]; then
        if [[ "${had_previous_release}" == yes ]]; then
            if printf '%s\n' "${previous_release_before}" >"${previous_release_file}.new.$$"; then
                mv -f -- "${previous_release_file}.new.$$" "${previous_release_file}" || true
            fi
        else
            rm -f -- "${previous_release_file}" || true
        fi
    fi
    if [[ "${previous_web_changed}" == yes ]]; then
        if [[ "${had_previous_web}" == yes ]]; then
            if printf '%s\n' "${previous_web_before}" >"${previous_web_file}.new.$$"; then
                mv -f -- "${previous_web_file}.new.$$" "${previous_web_file}" || true
            fi
        else
            rm -f -- "${previous_web_file}" || true
        fi
    fi
    if [[ "${switched_web}" == yes ]]; then
        rpg_atomic_symlink "${current_web}" "${RPG_WEB_CURRENT}" || true
    fi
    if [[ "${switched_app}" == yes ]]; then
        rpg_atomic_symlink "${current}" "${RPG_CURRENT_LINK}" || true
    fi
    if [[ "${switched_app}" == yes || "${switched_web}" == yes ]]; then
        if [[ "${control_plane_only}" == yes ]]; then
            restart_control_plane || true
        else
            rpg_restart_installed_services || true
        fi
    fi
    if [[ "${control_plane_only}" == yes ]]; then
        local proxy_after=""
        proxy_after="$(proxy_snapshot)" || status=1
        if [[ "${proxy_after}" != "${proxy_before}" ]]; then
            printf 'CRITICAL: proxy identity/configuration/listeners changed during rollback\n' >&2
            status=1
        fi
    fi
    exit "${status}"
}
trap restore_links ERR
rpg_atomic_symlink "${target}" "${RPG_CURRENT_LINK}"
switched_app=yes
rpg_atomic_symlink "${web_target}" "${RPG_WEB_CURRENT}"
switched_web=yes
if [[ "${control_plane_only}" == yes ]]; then
    restart_control_plane
    proxy_after="$(proxy_snapshot)"
    if [[ "${proxy_after}" != "${proxy_before}" ]]; then
        printf 'CRITICAL: proxy identity/configuration/listeners changed during rollback\n' >&2
        false
    fi
else
    rpg_restart_installed_services
fi
printf '%s\n' "${current}" >"${previous_release_file}.new.$$"
mv -f -- "${previous_release_file}.new.$$" "${previous_release_file}"
previous_release_changed=yes
printf '%s\n' "${current_web}" >"${previous_web_file}.new.$$"
mv -f -- "${previous_web_file}.new.$$" "${previous_web_file}"
previous_web_changed=yes
trap - ERR
rpg_note "Rolled back application and matching frontend to ${release}; database was not downgraded"
if [[ "${control_plane_only}" == yes ]]; then
    rpg_note "Proxy services were not restarted; identity and listeners verified"
fi
rpg_note "Use restore.sh with an explicit backup only when a database rollback is required"
