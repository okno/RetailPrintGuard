#!/usr/bin/env bash
# Build and activate an approved Git tag without restarting the POS/RCH relays.

set -Eeuo pipefail
IFS=$'\n\t'
umask 027

readonly DEFAULT_REPOSITORY="/srv/RetailPrintGuard"
readonly ACTIVE_RELEASE_LINK="/opt/retailprintguard/current"
readonly POS_PROXY_SERVICE="retailprintguard-pos-proxy.service"
readonly RCH_PROXY_SERVICE="retailprintguard-rch-proxy.service"

repository="${DEFAULT_REPOSITORY}"
remote="origin"
release=""

usage() {
    printf '%s\n' \
        'Usage: sudo update_control_plane_from_git.sh TAG [options]' \
        '  TAG                annotated release tag, for example v0.4.1' \
        '  --repo DIR         Git checkout (default: /srv/RetailPrintGuard)' \
        '  --remote NAME      approved Git remote (default: origin)' \
        '  --help             show this help' \
        '' \
        'The command builds the exact tag in an isolated worktree and invokes' \
        'update.sh --control-plane-only. It never falls back to a proxy restart.'
}

note() {
    printf '[RetailPrintGuard control-plane update] %s\n' "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

while (( $# > 0 )); do
    case "$1" in
        --repo)
            (( $# >= 2 )) || die "--repo requires a directory"
            repository="$2"
            shift 2
            ;;
        --remote)
            (( $# >= 2 )) || die "--remote requires a name"
            remote="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*) die "unknown argument: $1" ;;
        *)
            [[ -z "${release}" ]] || die "only one release tag may be supplied"
            release="$1"
            shift
            ;;
    esac
done

[[ "${EUID}" -eq 0 ]] || die "run this command as root"
[[ "${release}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
    die "TAG must be a stable semantic release such as v0.4.1"
[[ "${remote}" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid remote name"

for command in awk bash curl date flock git grep install mktemp node pnpm python3 readlink sleep sort ss systemctl tee; do
    require_command "${command}"
done

[[ -d "${repository}" ]] || die "repository directory does not exist: ${repository}"
repository="$(readlink -f -- "${repository}")"
git_safe=(git -c "safe.directory=${repository}" -C "${repository}")
[[ "$("${git_safe[@]}" rev-parse --is-inside-work-tree 2>/dev/null)" == true ]] || \
    die "not a Git worktree: ${repository}"

remote_url="$("${git_safe[@]}" remote get-url "${remote}")"
case "${remote_url}" in
    https://github.com/okno/RetailPrintGuard|https://github.com/okno/RetailPrintGuard.git|git@github.com:okno/RetailPrintGuard.git|ssh://git@github.com/okno/RetailPrintGuard.git) ;;
    *) die "remote ${remote} is not the approved okno/RetailPrintGuard repository" ;;
esac

[[ -L "${ACTIVE_RELEASE_LINK}" ]] || die "RetailPrintGuard has no active release"
active_release="$(readlink -f -- "${ACTIVE_RELEASE_LINK}")"
[[ -x "${active_release}/scripts/status.sh" ]] || die "active release is incomplete"

systemctl is-active --quiet "${POS_PROXY_SERVICE}" || die "POS proxy is not active"
systemctl is-active --quiet "${RCH_PROXY_SERVICE}" || die "RCH proxy is not active"
pos_pid_before="$(systemctl show "${POS_PROXY_SERVICE}" -p MainPID --value)"
rch_pid_before="$(systemctl show "${RCH_PROXY_SERVICE}" -p MainPID --value)"
pos_invocation_before="$(systemctl show "${POS_PROXY_SERVICE}" -p InvocationID --value)"
rch_invocation_before="$(systemctl show "${RCH_PROXY_SERVICE}" -p InvocationID --value)"
pos_started_before="$(systemctl show "${POS_PROXY_SERVICE}" -p ExecMainStartTimestampMonotonic --value)"
rch_started_before="$(systemctl show "${RCH_PROXY_SERVICE}" -p ExecMainStartTimestampMonotonic --value)"
[[ "${pos_pid_before}" =~ ^[1-9][0-9]*$ ]] || die "invalid POS proxy PID"
[[ "${rch_pid_before}" =~ ^[1-9][0-9]*$ ]] || die "invalid RCH proxy PID"
[[ -n "${pos_invocation_before}" && -n "${rch_invocation_before}" ]] || \
    die "cannot capture proxy invocation identity"
[[ "${pos_started_before}" =~ ^[1-9][0-9]*$ && \
   "${rch_started_before}" =~ ^[1-9][0-9]*$ ]] || die "invalid proxy start timestamp"

listener_snapshot() {
    local pos_pid="$1"
    local rch_pid="$2"
    ss -Hltpn | awk -v pos="pid=${pos_pid}," -v rch="pid=${rch_pid}," \
        'index($0, pos) || index($0, rch) { print $4 }' | LC_ALL=C sort -u
}

listeners_before="$(listener_snapshot "${pos_pid_before}" "${rch_pid_before}")"
[[ -n "${listeners_before}" ]] || die "no proxy listeners were found"
ss -Hltpn | grep -Fq "pid=${pos_pid_before}," || die "POS proxy has no listening socket"
ss -Hltpn | grep -Fq "pid=${rch_pid_before}," || die "RCH proxy has no listening socket"

require_healthy_status() {
    local status_script="$1"
    local attempts="${2:-1}"
    local payload=""
    local attempt
    for (( attempt=1; attempt<=attempts; attempt++ )); do
        if payload="$("${status_script}" --json 2>/dev/null)" && \
            printf '%s' "${payload}" | python3 -c \
                'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("healthy") is True else 1)'; then
            return 0
        fi
        (( attempt == attempts )) || sleep 2
    done
    return 1
}

require_ui_http_200() {
    local timeout_seconds="${1:-1}"
    local deadline=$((SECONDS + timeout_seconds))
    local http_code=""
    local remaining
    local request_timeout
    local sleep_seconds
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        request_timeout=2
        (( remaining >= request_timeout )) || request_timeout="${remaining}"
        if http_code="$(
            curl --silent --show-error --output /dev/null \
                --write-out '%{http_code}' --max-time "${request_timeout}" \
                http://127.0.0.1:8081/ 2>/dev/null
        )" && [[ "${http_code}" == 200 ]]; then
            return 0
        fi
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || break
        sleep_seconds=2
        (( remaining >= sleep_seconds )) || sleep_seconds="${remaining}"
        sleep "${sleep_seconds}"
    done
    return 1
}

require_healthy_status "${active_release}/scripts/status.sh" 1 || \
    die "active installation is not healthy; update refused"

# Hold the same maintenance lock across acquisition, build, backup and
# activation.  update.sh/backup.sh/install.sh inherit descriptor 9 and the
# marker, so there is no race window between their individual phases.
install -d -m 0755 -- /run/lock
exec 9>/run/lock/retailprintguard-maintenance.lock
flock -n 9 || die "another RetailPrintGuard maintenance operation is active"
export RPG_MAINTENANCE_LOCK_HELD=1
export GIT_TERMINAL_PROMPT=0

log_dir="/var/log/retailprintguard/updates"
install -d -m 0750 -o root -g root -- "${log_dir}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_file="${log_dir}/${release}-${timestamp}.log"
install -m 0600 -o root -g root /dev/null "${log_file}"
exec > >(tee -a "${log_file}") 2>&1

note "Approved remote: ${remote_url}"
note "Active release: ${active_release}"
note "Requested tag: ${release}"
note "Proxy PIDs before update: POS=${pos_pid_before}, RCH=${rch_pid_before}"
note "Proxy invocation IDs before update: POS=${pos_invocation_before}, RCH=${rch_invocation_before}"
note "Listener snapshot before update:"
printf '%s\n' "${listeners_before}"
note "Log: ${log_file}"

temporary_root="$(mktemp -d /var/tmp/retailprintguard-control-plane.XXXXXXXX)"
candidate_worktree="${temporary_root}/source"
worktree_registered=no

cleanup() {
    local result=$?
    local pos_pid_after=""
    local rch_pid_after=""
    local pos_invocation_after=""
    local rch_invocation_after=""
    local pos_started_after=""
    local rch_started_after=""
    local listeners_at_exit=""
    trap - EXIT
    set +e
    if [[ "${worktree_registered}" == yes ]]; then
        "${git_safe[@]}" worktree remove --force -- "${candidate_worktree}" >/dev/null 2>&1
        "${git_safe[@]}" worktree prune >/dev/null 2>&1
    fi
    case "${temporary_root}" in
        /var/tmp/retailprintguard-control-plane.*)
            [[ ! -e "${candidate_worktree}" ]] && rmdir -- "${temporary_root}" 2>/dev/null
            ;;
    esac
    pos_pid_after="$(systemctl show "${POS_PROXY_SERVICE}" -p MainPID --value 2>/dev/null)"
    rch_pid_after="$(systemctl show "${RCH_PROXY_SERVICE}" -p MainPID --value 2>/dev/null)"
    pos_invocation_after="$(systemctl show "${POS_PROXY_SERVICE}" -p InvocationID --value 2>/dev/null)"
    rch_invocation_after="$(systemctl show "${RCH_PROXY_SERVICE}" -p InvocationID --value 2>/dev/null)"
    pos_started_after="$(systemctl show "${POS_PROXY_SERVICE}" -p ExecMainStartTimestampMonotonic --value 2>/dev/null)"
    rch_started_after="$(systemctl show "${RCH_PROXY_SERVICE}" -p ExecMainStartTimestampMonotonic --value 2>/dev/null)"
    if [[ "${pos_pid_after}" =~ ^[1-9][0-9]*$ && \
          "${rch_pid_after}" =~ ^[1-9][0-9]*$ ]]; then
        listeners_at_exit="$(listener_snapshot "${pos_pid_after}" "${rch_pid_after}")"
    fi
    if [[ "${pos_pid_after}" != "${pos_pid_before}" || \
          "${rch_pid_after}" != "${rch_pid_before}" || \
          "${pos_invocation_after}" != "${pos_invocation_before}" || \
          "${rch_invocation_after}" != "${rch_invocation_before}" || \
          "${pos_started_after}" != "${pos_started_before}" || \
          "${rch_started_after}" != "${rch_started_before}" || \
          "${listeners_at_exit}" != "${listeners_before}" ]] || \
          ! systemctl is-active --quiet "${POS_PROXY_SERVICE}" || \
          ! systemctl is-active --quiet "${RCH_PROXY_SERVICE}"; then
        printf 'CRITICAL: proxy identity, active state or listeners changed (POS %s -> %s, RCH %s -> %s)\n' \
            "${pos_pid_before}" "${pos_pid_after}" "${rch_pid_before}" "${rch_pid_after}" >&2
        result=1
    fi
    if (( result != 0 )); then
        printf 'FAILED: control-plane update did not complete. Proxy restart fallback was not attempted.\n' >&2
        printf 'Inspect %s and run the installed diagnose.sh before any manual recovery.\n' \
            "${log_file}" >&2
    fi
    exit "${result}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

note "Fetching immutable release tags"
"${git_safe[@]}" fetch --prune "${remote}" \
    "refs/heads/main:refs/remotes/${remote}/main"
"${git_safe[@]}" fetch --prune --tags "${remote}"
remote_tag_refs="$("${git_safe[@]}" ls-remote --exit-code --tags "${remote}" \
    "refs/tags/${release}" "refs/tags/${release}^{}")" || \
    die "tag does not exist on approved remote: ${release}"
[[ "$("${git_safe[@]}" cat-file -t "refs/tags/${release}" 2>/dev/null)" == tag ]] || \
    die "release reference must be an annotated tag: ${release}"
candidate_commit="$("${git_safe[@]}" rev-parse --verify "refs/tags/${release}^{commit}")"
remote_commit="$(printf '%s\n' "${remote_tag_refs}" | awk '$2 ~ /\^\{\}$/ { print $1 }')"
[[ -n "${remote_commit}" && "${remote_commit}" == "${candidate_commit}" ]] || \
    die "local and remote tag targets do not match for ${release}"
"${git_safe[@]}" merge-base --is-ancestor "${candidate_commit}" \
    "refs/remotes/${remote}/main" || die "release tag is not contained in ${remote}/main"
note "Resolved ${release} to ${candidate_commit}"

"${git_safe[@]}" worktree add --detach -- "${candidate_worktree}" "${candidate_commit}"
worktree_registered=yes
[[ -x "${candidate_worktree}/scripts/update.sh" && \
   -f "${candidate_worktree}/frontend/pnpm-lock.yaml" ]] || \
    die "candidate release is missing update or frontend assets"

note "Verifying release identity"
python3 - "${candidate_worktree}" "${release#v}" <<'PY'
import ast
import json
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
expected = sys.argv[2]
with (root / "pyproject.toml").open("rb") as stream:
    python_version = tomllib.load(stream)["project"]["version"]
frontend_version = json.loads(
    (root / "frontend/package.json").read_text(encoding="utf-8")
)["version"]
tree = ast.parse((root / "src/retailprintguard/__init__.py").read_text(encoding="utf-8"))
package_version = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__version__"
        for target in node.targets
    ):
        package_version = ast.literal_eval(node.value)
versions = {python_version, frontend_version, package_version}
if versions != {expected}:
    raise SystemExit(
        f"version mismatch: tag={expected}, python={python_version}, "
        f"package={package_version}, frontend={frontend_version}"
    )
PY
expected_pnpm="$(python3 - "${candidate_worktree}/frontend/package.json" <<'PY'
import json
import sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["packageManager"].split("@", 1)[1])
PY
)"
[[ "$(pnpm --version)" == "${expected_pnpm}" ]] || die \
    "pnpm version mismatch: release requires ${expected_pnpm}"

note "Checking the complete proxy dependency closure before build or backup"
bash -c 'source "$1"; rpg_assert_data_plane_unchanged "$2" "$3"' _ \
    "${candidate_worktree}/scripts/lib.sh" "${active_release}" "${candidate_worktree}"

note "Installing locked frontend dependencies"
(
    cd -- "${candidate_worktree}/frontend"
    pnpm install --frozen-lockfile
    pnpm lint
    pnpm test
    pnpm build
)
[[ -s "${candidate_worktree}/frontend/dist/index.html" ]] || \
    die "frontend production build is missing"

note "Activating only the control plane; POS/RCH proxy services are excluded"
"${candidate_worktree}/scripts/update.sh" \
    --control-plane-only \
    --frontend-dir "${candidate_worktree}/frontend/dist"

pos_pid_after="$(systemctl show "${POS_PROXY_SERVICE}" -p MainPID --value)"
rch_pid_after="$(systemctl show "${RCH_PROXY_SERVICE}" -p MainPID --value)"
pos_invocation_after="$(systemctl show "${POS_PROXY_SERVICE}" -p InvocationID --value)"
rch_invocation_after="$(systemctl show "${RCH_PROXY_SERVICE}" -p InvocationID --value)"
pos_started_after="$(systemctl show "${POS_PROXY_SERVICE}" -p ExecMainStartTimestampMonotonic --value)"
rch_started_after="$(systemctl show "${RCH_PROXY_SERVICE}" -p ExecMainStartTimestampMonotonic --value)"
[[ "${pos_pid_after}" == "${pos_pid_before}" ]] || die "POS proxy PID changed"
[[ "${rch_pid_after}" == "${rch_pid_before}" ]] || die "RCH proxy PID changed"
[[ "${pos_invocation_after}" == "${pos_invocation_before}" ]] || die "POS proxy invocation changed"
[[ "${rch_invocation_after}" == "${rch_invocation_before}" ]] || die "RCH proxy invocation changed"
[[ "${pos_started_after}" == "${pos_started_before}" ]] || die "POS proxy start time changed"
[[ "${rch_started_after}" == "${rch_started_before}" ]] || die "RCH proxy start time changed"
listeners_after="$(listener_snapshot "${pos_pid_after}" "${rch_pid_after}")"
[[ "${listeners_after}" == "${listeners_before}" ]] || \
    die "proxy listener set changed during update"

active_release_after="$(readlink -f -- "${ACTIVE_RELEASE_LINK}")"
require_healthy_status "${active_release_after}/scripts/status.sh" 30 || \
    die "new control plane did not become healthy within 60 seconds"
require_ui_http_200 60 || \
    die "new web UI did not return HTTP 200 within 60 seconds"

note "SUCCESS: ${release} is active"
note "Proxy PIDs preserved: POS=${pos_pid_after}, RCH=${rch_pid_after}"
note "Proxy listeners preserved:"
printf '%s\n' "${listeners_after}"
note "No proxy stop/start/restart fallback was attempted"
note "Verified log: ${log_file}"
