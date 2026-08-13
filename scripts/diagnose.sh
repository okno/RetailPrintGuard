#!/usr/bin/env bash
# Bounded, payload-free diagnostics suitable for a support ticket.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

printf 'generated_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'kernel=%s\n' "$(uname -srvmo)"
if [[ -r /etc/os-release ]]; then
    awk -F= '$1 == "PRETTY_NAME" {print "os=" $2}' /etc/os-release
fi
printf 'current_release=%s\n' "$(rpg_current_release)"
if [[ -f "${RPG_CONFIG_PATH}" ]]; then
    printf 'config_sha256=%s\n' "$(sha256sum "${RPG_CONFIG_PATH}" | awk '{print $1}')"
fi
"${SCRIPT_DIR}/healthcheck.sh" --json || true
printf '%s\n' '--- sockets ---'
ss -ltn 2>/dev/null | head -n 100 || true
printf '%s\n' '--- failed units ---'
systemctl --failed --no-pager --plain 2>/dev/null | head -n 100 || true
printf '%s\n' '--- recent service logs (payload logging is disabled by configuration) ---'
journalctl --no-pager --since '-30 minutes' -n 300 \
    -u 'retailprintguard-*.service' 2>/dev/null || true
