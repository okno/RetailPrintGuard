#!/usr/bin/env bash
# Static release gate for deployment assets; safe on a development workstation.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

while IFS= read -r -d '' script; do
    bash -n "${script}"
done < <(find "${ROOT}/scripts" -maxdepth 1 -type f -name '*.sh' -print0)
printf 'PASS: bash syntax\n'

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -x "${ROOT}"/scripts/*.sh
    printf 'PASS: ShellCheck\n'
else
    printf 'SKIP: ShellCheck is unavailable\n'
fi

if command -v systemd-analyze >/dev/null 2>&1; then
    temporary="$(mktemp -d)"
    cleanup() { rm -rf -- "${temporary}"; }
    trap cleanup EXIT
    cp -- "${ROOT}"/systemd/* "${temporary}/"
    chmod 0644 "${temporary}"/*
    sed -E -i \
        -e 's#^User=.*#User=root#' \
        -e 's#^Group=.*#Group=root#' \
        -e 's#^SupplementaryGroups=.*#SupplementaryGroups=#' \
        -e 's#^EnvironmentFile=.*#EnvironmentFile=-/tmp/nonexistent.env#' \
        -e 's#^ExecStartPre=.*#ExecStartPre=/bin/true#' \
        -e 's#^ExecStart=.*#ExecStart=/bin/true#' \
        -e 's#^ReadWritePaths=.*#ReadWritePaths=-/tmp#' \
        -e 's#^ReadOnlyPaths=.*#ReadOnlyPaths=-/tmp#' \
        "${temporary}"/*.service
    SYSTEMD_UNIT_PATH="${temporary}:/usr/lib/systemd/system:/lib/systemd/system" \
        systemd-analyze verify "${temporary}"/*.service "${temporary}"/*.timer \
            "${temporary}"/*.target
    printf 'PASS: systemd-analyze verify\n'
    trap - EXIT
    rm -rf -- "${temporary}"
else
    printf 'SKIP: systemd-analyze is unavailable\n'
fi

if command -v nginx >/dev/null 2>&1; then
    printf 'NOTICE: nginx syntax is verified by install.sh after deployment paths exist\n'
fi
