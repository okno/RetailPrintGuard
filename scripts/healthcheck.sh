#!/usr/bin/env bash
# Read-only production health summary. It never prints credentials or payload bytes.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

json=no
if [[ "${1:-}" == "--json" ]]; then
    json=yes
    shift
fi
(( $# == 0 )) || rpg_die "usage: $0 [--json]"

failures=0
services_json=()
for service in "${RPG_SERVICES[@]}"; do
    state="$(systemctl is-active "${service}" 2>/dev/null || true)"
    [[ "${state}" == active ]] || failures=$((failures + 1))
    services_json+=("$(printf '{\"name\":\"%s\",\"state\":\"%s\"}' "${service}" "${state}")")
done

config_state=invalid
if [[ -x "${RPG_CURRENT_LINK}/.venv/bin/python" && -f "${RPG_CONFIG_PATH}" ]]; then
    if "${RPG_CURRENT_LINK}/.venv/bin/python" \
        "${RPG_CURRENT_LINK}/scripts/validate_site_config.py" \
        --config "${RPG_CONFIG_PATH}" --require-deployment-layout \
        --require-assigned-listeners >/dev/null 2>&1; then
        config_state=valid
    fi
fi
[[ "${config_state}" == valid ]] || failures=$((failures + 1))

database_state=unreachable
if command -v mariadb-admin >/dev/null 2>&1 && \
    mariadb-admin --protocol=socket ping >/dev/null 2>&1; then
    database_state=reachable
fi
[[ "${database_state}" == reachable ]] || failures=$((failures + 1))

api_state=unreachable
if command -v curl >/dev/null 2>&1 && \
    curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8080/api/v1/system/health >/dev/null 2>&1; then
    api_state=reachable
fi
[[ "${api_state}" == reachable ]] || failures=$((failures + 1))

spool_bytes="$(du -sb -- "${RPG_SPOOL_ROOT}" 2>/dev/null | awk '{print $1}' || printf '0')"
ready_jobs="$(find "${RPG_SPOOL_ROOT}" -type f -name .ready 2>/dev/null | wc -l)"
partial_jobs="$(find "${RPG_SPOOL_ROOT}" -type d -name '*.partial' 2>/dev/null | wc -l)"
free_bytes="$(df -PB1 --output=avail "${RPG_DATA_ROOT}" 2>/dev/null | tail -n 1 | tr -d ' ')"
spool_bytes="${spool_bytes:-0}"
ready_jobs="${ready_jobs//[[:space:]]/}"
partial_jobs="${partial_jobs//[[:space:]]/}"
free_bytes="${free_bytes:-0}"

if [[ "${json}" == yes ]]; then
    joined="$(IFS=,; printf '%s' "${services_json[*]}")"
    printf '{"healthy":%s,"failures":%d,"config":"%s","database":"%s","api":"%s","spool_bytes":%s,"ready_jobs":%s,"partial_jobs":%s,"free_bytes":%s,"services":[%s]}\n' \
        "$([[ "${failures}" -eq 0 ]] && printf true || printf false)" \
        "${failures}" "${config_state}" "${database_state}" "${api_state}" \
        "${spool_bytes}" "${ready_jobs}" "${partial_jobs}" "${free_bytes}" "${joined}"
else
    printf 'configuration: %s\ndatabase: %s\napi: %s\n' \
        "${config_state}" "${database_state}" "${api_state}"
    printf 'spool_bytes: %s\nready_jobs: %s\npartial_jobs: %s\nfree_bytes: %s\n' \
        "${spool_bytes}" "${ready_jobs}" "${partial_jobs}" "${free_bytes}"
    for item in "${services_json[@]}"; do
        printf '%s\n' "${item}"
    done
fi
(( failures == 0 ))
