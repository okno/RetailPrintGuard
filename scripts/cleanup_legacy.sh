#!/usr/bin/env bash
# Verified, non-destructive cutover from standalone printproxy/commercialRCHproxy.

set -Eeuo pipefail
IFS=$'\n\t'
umask 0077

readonly CLEANUP_LOCK="/run/lock/retailprintguard-legacy-cleanup.lock"
readonly DEFAULT_BACKUP_ROOT="/var/backups/retailprintguard/legacy"
readonly -a DATA_SERVICES=(
    printproxy.service
    commercialrchproxy-dumper.service
    commercialrchproxy-parser.service
    commercialrchproxy.service
)
readonly -a LEGACY_SERVICES=(
    printproxy.service
    printproxy-vip-watch.timer
    printproxy-vip-watch.service
    printproxy-firewall.service
    printproxy-vip.service
    commercialrchproxy-dumper.service
    commercialrchproxy-parser.service
    commercialrchproxy.service
    commercialrchproxy-secondary-ip.service
)
readonly -a LEGACY_UNIT_FILES=(
    /etc/systemd/system/printproxy.service
    /etc/systemd/system/printproxy-vip.service
    /etc/systemd/system/printproxy-firewall.service
    /etc/systemd/system/printproxy-vip-watch.service
    /etc/systemd/system/printproxy-vip-watch.timer
    /etc/systemd/system/commercialrchproxy.service
    /etc/systemd/system/commercialrchproxy-dumper.service
    /etc/systemd/system/commercialrchproxy-parser.service
    /etc/systemd/system/commercialrchproxy-secondary-ip.service
)

execute=no
network_handover=no
firewall_handover=no
backup_root="${DEFAULT_BACKUP_ROOT}"
printproxy_uninstaller=""
rch_uninstaller=""
network_reapply_helper=""
removal_started=no
services_stopped=no
recovery_archive=""
declare -a active_before=()
declare -a sources=()
declare -a expected_listeners=()
declare -A source_seen=()
declare -A listener_seen=()

note() { printf '[RetailPrintGuard legacy cleanup] %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/cleanup_legacy.sh [OPTIONS]

The default is a read-only inventory.  --execute creates and verifies a
root-only recovery archive, then invokes the verified legacy uninstallers.
Configuration, evidence, logs, service accounts and old backups are preserved.

Options:
  --execute
  --backup-dir DIR
  --printproxy-uninstaller FILE
  --rch-uninstaller FILE
  --network-handover-confirmed   required when legacy services own listener IPs
  --network-reapply-helper FILE  root-owned site helper run after VIP removal
  --firewall-handover-confirmed  required when printproxy owns its nft table
  -h, --help

Before confirming a handover, configure every listener address persistently
with the host's real network manager and install an approved replacement host
firewall.  This script never writes network-manager configuration.
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --execute) execute=yes; shift ;;
        --backup-dir)
            (( $# >= 2 )) || die "--backup-dir requires a directory"
            backup_root="$2"; shift 2
            ;;
        --printproxy-uninstaller)
            (( $# >= 2 )) || die "--printproxy-uninstaller requires a file"
            printproxy_uninstaller="$2"; shift 2
            ;;
        --rch-uninstaller)
            (( $# >= 2 )) || die "--rch-uninstaller requires a file"
            rch_uninstaller="$2"; shift 2
            ;;
        --network-reapply-helper)
            (( $# >= 2 )) || die "--network-reapply-helper requires a file"
            network_reapply_helper="$2"; shift 2
            ;;
        --network-handover-confirmed) network_handover=yes; shift ;;
        --firewall-handover-confirmed) firewall_handover=yes; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

for command_name in python3 realpath tar gzip sha256sum systemctl stat ss ip du df; do
    command -v "${command_name}" >/dev/null 2>&1 || \
        die "required command is missing: ${command_name}"
done
if [[ "${execute}" == yes ]]; then
    [[ "${EUID}" -eq 0 ]] || die "--execute must run as root"
    command -v flock >/dev/null 2>&1 || die "required command is missing: flock"
    install -d -m 0755 -- /run/lock
    exec 9>"${CLEANUP_LOCK}"
    flock -n 9 || die "another legacy cleanup is active"
fi

backup_root="$(realpath -m -- "${backup_root}")"
[[ "${backup_root}" == /* && "${backup_root}" != / ]] || \
    die "backup directory must be an absolute non-root path"

config_value() {
    local config_path="$1"
    local wanted="$2"
    [[ -r "${config_path}" && -f "${config_path}" && ! -L "${config_path}" ]] || return 0
    python3 - "${config_path}" "${wanted}" <<'PY'
from pathlib import Path
import sys

path, wanted = Path(sys.argv[1]), sys.argv[2]
found = []
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() != wanted:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    found.append(value)
if len(found) > 1:
    raise SystemExit(f"duplicate key {wanted} in {path}")
if found:
    print(found[0])
PY
}

add_source() {
    local candidate="$1"
    local normalized
    [[ -n "${candidate}" && "${candidate}" == /* ]] || return 0
    normalized="$(realpath -m -- "${candidate}")"
    case "${normalized}" in
        /|/boot|/dev|/etc|/home|/opt|/proc|/root|/run|/sys|/usr|/var)
            die "refusing unsafe broad source: ${normalized}"
            ;;
    esac
    case "${backup_root}/" in
        "${normalized}/"*) die "backup destination is inside source: ${normalized}" ;;
    esac
    [[ -e "${normalized}" || -L "${normalized}" ]] || return 0
    [[ ! ( -d "${normalized}" && -L "${normalized}" ) ]] || \
        die "refusing symlinked source directory: ${normalized}"
    if [[ -z "${source_seen[${normalized}]:-}" ]]; then
        source_seen["${normalized}"]=1
        sources+=("${normalized}")
    fi
}

add_listener() {
    local value="$1"
    local listener
    IFS=',' read -r -a values <<<"${value}"
    for listener in "${values[@]}"; do
        listener="${listener//[[:space:]]/}"
        [[ -n "${listener}" ]] || continue
        if [[ -z "${listener_seen[${listener}]:-}" ]]; then
            listener_seen["${listener}"]=1
            expected_listeners+=("${listener}")
        fi
    done
}

secure_executable() {
    local candidate="$1"
    local mode
    [[ -f "${candidate}" && ! -L "${candidate}" && -x "${candidate}" ]] || \
        die "expected a regular executable: ${candidate}"
    [[ "$(stat -c '%u' -- "${candidate}")" == 0 ]] || \
        die "executable must be root-owned: ${candidate}"
    mode="$(stat -c '%a' -- "${candidate}")"
    (( (8#${mode} & 8#022) == 0 )) || \
        die "executable must not be group/world writable: ${candidate}"
}

find_executable() {
    local explicit="$1"
    shift
    local candidate
    if [[ -n "${explicit}" ]]; then
        printf '%s\n' "$(realpath -m -- "${explicit}")"
        return
    fi
    for candidate in "$@"; do
        if [[ -f "${candidate}" && ! -L "${candidate}" && -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return
        fi
    done
}

readonly -a STANDARD_SOURCES=(
    /etc/printproxy /opt/printproxy /var/lib/printproxy /var/log/printproxy
    /usr/local/sbin/printproxyctl /usr/local/libexec/printproxy-vip
    /usr/local/libexec/printproxy-firewall /etc/logrotate.d/printproxy
    /etc/systemd/system/printproxy.service.d
    /etc/commercialrchproxy /opt/commercialrchproxy
    /var/lib/commercialrchproxy /var/log/commercialrchproxy
    /usr/local/libexec/commercialrchproxy
    /usr/local/libexec/commercialrchproxy-network
    /etc/systemd/system/commercialrchproxy.service.d
    /etc/systemd/system/commercialrchproxy-dumper.service.d
    /run/commercialrchproxy-secondary-ip
)
for candidate in "${STANDARD_SOURCES[@]}" "${LEGACY_UNIT_FILES[@]}"; do
    add_source "${candidate}"
done
for key in DATA_DIR SPOOL_DIR LOG_DIR HMAC_KEY_FILE; do
    add_source "$(config_value /etc/printproxy/printproxy.conf "${key}")"
done
for key in OUTPUT_DIR LOG_DIR; do
    add_source "$(config_value /etc/commercialrchproxy/commercialrchproxy.conf "${key}")"
done

printproxy_installed=no
rch_installed=no
# Preserved /etc state is evidence, not an installed executable runtime.  This
# distinction makes a second cleanup invocation a harmless no-op.
if [[ -e /opt/printproxy || -e /etc/systemd/system/printproxy.service || \
      -e /usr/local/sbin/printproxyctl || \
      -e /usr/local/libexec/printproxy-vip || \
      -e /usr/local/libexec/printproxy-firewall ]]; then
    printproxy_installed=yes
fi
if [[ -e /opt/commercialrchproxy || \
      -e /etc/systemd/system/commercialrchproxy-dumper.service || \
      -e /etc/systemd/system/commercialrchproxy-parser.service || \
      -e /usr/local/libexec/commercialrchproxy || \
      -e /usr/local/libexec/commercialrchproxy-network ]]; then
    rch_installed=yes
fi

if [[ "${printproxy_installed}" == yes ]]; then
    printproxy_uninstaller="$(find_executable "${printproxy_uninstaller}" \
        /usr/local/libexec/printproxy/uninstall.sh \
        /srv/printproxy/uninstall.sh \
        /root/printproxy/uninstall.sh)"
    [[ -n "${printproxy_uninstaller}" ]] || \
        die "printproxy is installed; provide --printproxy-uninstaller from its frozen repository"
    secure_executable "${printproxy_uninstaller}"
    add_source "${printproxy_uninstaller}"
fi
if [[ "${rch_installed}" == yes ]]; then
    rch_uninstaller="$(find_executable "${rch_uninstaller}" \
        /usr/local/libexec/commercialrchproxy/uninstall.sh \
        /srv/commercialRCHproxy/scripts/uninstall.sh \
        /root/commercialRCHproxy/scripts/uninstall.sh)"
    [[ -n "${rch_uninstaller}" ]] || \
        die "commercialRCHproxy is installed; provide --rch-uninstaller"
    secure_executable "${rch_uninstaller}"
    add_source "${rch_uninstaller}"
fi

rch_network_helper=/usr/local/libexec/commercialrchproxy-network/manage_secondary_ip.sh
if [[ -e "${rch_network_helper}" || -L "${rch_network_helper}" ]]; then
    secure_executable "${rch_network_helper}"
fi

add_listener "$(config_value /etc/printproxy/install-state VIP_LIST)"
if (( ${#expected_listeners[@]} == 0 )); then
    add_listener "$(config_value /etc/printproxy/printproxy.conf VIP_LIST)"
fi
if (( ${#expected_listeners[@]} == 0 )); then
    add_listener "$(config_value /etc/printproxy/install-state VIP)"
fi
if (( ${#expected_listeners[@]} == 0 )); then
    add_listener "$(config_value /etc/printproxy/printproxy.conf LISTEN_IP)"
fi
add_listener "$(config_value /etc/commercialrchproxy/commercialrchproxy.conf LISTEN_IP)"

network_owned=no
firewall_owned=no
print_owned="$(config_value /etc/printproxy/install-state VIP_OWNED_LIST)"
rch_owned="$(config_value /run/commercialrchproxy-secondary-ip/state OWNED)"
[[ "${print_owned}" == *yes* || "${rch_owned}" == 1 ]] && network_owned=yes
[[ "$(config_value /etc/printproxy/install-state FIREWALL_OWNED)" == yes ]] && firewall_owned=yes
if [[ -n "${network_reapply_helper}" ]]; then
    network_reapply_helper="$(realpath -m -- "${network_reapply_helper}")"
    secure_executable "${network_reapply_helper}"
    add_source "${network_reapply_helper}"
fi

legacy_present=no
[[ "${printproxy_installed}" == yes || "${rch_installed}" == yes ]] && legacy_present=yes
for service in "${LEGACY_SERVICES[@]}"; do
    if systemctl is-active --quiet "${service}" 2>/dev/null || \
       systemctl is-enabled --quiet "${service}" 2>/dev/null; then
        legacy_present=yes
    fi
done
if [[ "${legacy_present}" == no ]]; then
    note "No legacy executable installation was found; the host is already clean."
    exit 0
fi
(( ${#expected_listeners[@]} > 0 )) || \
    die "legacy runtime exists but no listener address could be verified from protected state/config"

note "Recovery sources (old backup directories remain separately preserved):"
printf '  %s\n' "${sources[@]}"
note "Expected listener addresses after the network handover: ${expected_listeners[*]:-none discovered}"
note "Legacy network ownership detected: ${network_owned}; firewall ownership: ${firewall_owned}"
for service in "${LEGACY_SERVICES[@]}"; do
    printf '  %-48s active=%-8s enabled=%s\n' \
        "${service}" \
        "$(systemctl is-active "${service}" 2>/dev/null || true)" \
        "$(systemctl is-enabled "${service}" 2>/dev/null || true)"
done
if [[ "${execute}" != yes ]]; then
    note "DRY RUN only. Review the inventory and repeat with --execute."
    exit 0
fi

[[ "${network_owned}" != yes || "${network_handover}" == yes ]] || \
    die "legacy owns listener IPs; configure them persistently, then pass --network-handover-confirmed"
[[ "${network_owned}" != yes || -n "${network_reapply_helper}" ]] || \
    die "legacy owns listener IPs; provide a reviewed --network-reapply-helper"
[[ "${firewall_owned}" != yes || "${firewall_handover}" == yes ]] || \
    die "legacy owns its nft table; install an approved replacement, then pass --firewall-handover-confirmed"
for service in retailprintguard-pos-proxy.service retailprintguard-rch-proxy.service; do
    systemctl is-active --quiet "${service}" 2>/dev/null && \
        die "new proxy is already active: ${service}"
done

for service in "${DATA_SERVICES[@]}"; do
    pid="$(systemctl show --property MainPID --value "${service}" 2>/dev/null || true)"
    if [[ "${pid}" =~ ^[1-9][0-9]*$ ]] && \
       ss -Htnp 2>/dev/null | grep -Fq "pid=${pid},"; then
        die "active TCP session belongs to ${service}; wait for quiescence"
    fi
done

install -d -m 0700 -o root -g root -- "${backup_root}"
required_bytes=67108864
for candidate in "${sources[@]}"; do
    size="$(du -sx --block-size=1 -- "${candidate}" | awk '{print $1}')"
    required_bytes=$((required_bytes + size))
done
available_bytes="$(df --output=avail -B1 -- "${backup_root}" | awk 'NR==2 {print $1}')"
(( available_bytes >= required_bytes )) || \
    die "insufficient backup space: need at least ${required_bytes} bytes, have ${available_bytes}"

for service in "${LEGACY_SERVICES[@]}"; do
    systemctl is-active --quiet "${service}" 2>/dev/null && active_before+=("${service}")
done
restore_on_failure() {
    local rc=$?
    trap - EXIT INT TERM HUP
    if [[ "${rc}" -ne 0 && "${removal_started}" == no && \
          "${services_stopped}" == yes && ${#active_before[@]} -gt 0 ]]; then
        printf 'WARNING: cleanup failed before removal; restarting previously active services.\n' >&2
        systemctl start "${active_before[@]}" >/dev/null 2>&1 || \
            printf 'CRITICAL: some legacy services could not be restarted.\n' >&2
    elif [[ "${rc}" -ne 0 && "${removal_started}" == yes ]]; then
        printf 'CRITICAL: cleanup failed after removal began; do not restart blindly. Recovery: %s\n' \
            "${recovery_archive:-not-created}" >&2
    fi
    exit "${rc}"
}
trap restore_on_failure EXIT INT TERM HUP

# Stop the reconciler before touching a VIP.  Data services are then stopped
# for a consistent evidence archive.
systemctl stop printproxy-vip-watch.timer printproxy-vip-watch.service 2>/dev/null || true
for service in "${DATA_SERVICES[@]}"; do
    systemctl stop "${service}" 2>/dev/null || true
done
services_stopped=yes
for service in "${DATA_SERVICES[@]}"; do
    systemctl is-active --quiet "${service}" 2>/dev/null && \
        die "legacy data service did not stop: ${service}"
done

if [[ "${printproxy_installed}" == yes && -x /usr/local/sbin/printproxyctl ]]; then
    note "Verifying the stopped printproxy evidence ledger and HMAC."
    /usr/local/sbin/printproxyctl \
        --config /etc/printproxy/printproxy.conf verify
fi
if [[ "${rch_installed}" == yes && \
      -x /usr/local/libexec/commercialrchproxy/check_config.sh ]]; then
    note "Verifying the installed commercialRCHproxy configuration."
    /usr/local/libexec/commercialrchproxy/check_config.sh
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
bundle_dir="$(mktemp -d "${backup_root}/legacy-${timestamp}.XXXXXXXX")"
chmod 0700 -- "${bundle_dir}"
snapshot_dir="${bundle_dir}/snapshot"
install -d -m 0700 -- "${snapshot_dir}"
{
    printf 'created_at_utc=%s\n' "${timestamp}"
    printf 'hostname=%s\n' "$(hostname)"
    printf 'previously_active=%s\n' "${active_before[*]:-none}"
    printf 'expected_listener=%s\n' "${expected_listeners[@]:-none}"
} >"${snapshot_dir}/cleanup-state.txt"
ip -j -4 address show >"${snapshot_dir}/ip-address.json"
ip -j -4 route show table all >"${snapshot_dir}/ip-route.json"
ss -Hltunp >"${snapshot_dir}/sockets.txt" 2>&1 || true
if command -v nft >/dev/null 2>&1; then
    nft list ruleset >"${snapshot_dir}/nftables.txt" 2>&1 || true
fi
for service in "${LEGACY_SERVICES[@]}"; do
    systemctl show "${service}" >"${snapshot_dir}/${service}.show" 2>&1 || true
    systemctl cat "${service}" >"${snapshot_dir}/${service}.unit" 2>&1 || true
    journalctl -u "${service}" -n 2000 --no-pager \
        >"${snapshot_dir}/${service}.journal" 2>&1 || true
done
getent passwd printproxy commercialrchproxy >"${snapshot_dir}/accounts.txt" 2>&1 || true
getent group printproxy commercialrchproxy >>"${snapshot_dir}/accounts.txt" 2>&1 || true

python3 - "${snapshot_dir}/files.jsonl" "${snapshot_dir}" "${sources[@]}" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

output = Path(sys.argv[1])
roots = [Path(value) for value in sys.argv[2:]]
with output.open("w", encoding="utf-8", newline="\n") as stream:
    for root in roots:
        entries = [root]
        if root.is_dir() and not root.is_symlink():
            entries.extend(sorted(root.rglob("*")))
        for entry in entries:
            if entry == output:
                continue
            info = entry.lstat()
            record = {
                "path": str(entry),
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "size": info.st_size,
                "type": "other",
            }
            if entry.is_symlink():
                record.update(type="symlink", target=os.readlink(entry))
            elif entry.is_file():
                digest = hashlib.sha256()
                with entry.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                record.update(type="file", sha256=digest.hexdigest())
            elif entry.is_dir():
                record["type"] = "directory"
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
PY

archive_name="legacy-print-proxies.tar.gz"
archive_path="${bundle_dir}/${archive_name}"
declare -a relative_sources=()
for candidate in "${sources[@]}"; do relative_sources+=("${candidate#/}"); done
tar --create --gzip --numeric-owner --file "${archive_path}" \
    --directory "${bundle_dir}" snapshot \
    --directory / -- "${relative_sources[@]}"
gzip --test -- "${archive_path}"
tar --list --gzip --file "${archive_path}" >/dev/null
archive_digest="$(sha256sum -- "${archive_path}" | awk '{print $1}')"
printf '%s  %s\n' "${archive_digest}" "${archive_name}" >"${archive_path}.sha256"
(
    cd -- "${bundle_dir}"
    sha256sum --check --strict "${archive_name}.sha256"
)
chmod 0600 -- "${archive_path}" "${archive_path}.sha256"
recovery_archive="${archive_path}"

removal_started=yes
if [[ -e "${rch_network_helper}" || -L "${rch_network_helper}" ]]; then
    "${rch_network_helper}" uninstall --yes
fi
if [[ "${rch_installed}" == yes ]]; then
    "${rch_uninstaller}"
fi
if [[ "${printproxy_installed}" == yes ]]; then
    "${printproxy_uninstaller}"
fi
if [[ "${network_owned}" == yes ]]; then
    note "Reapplying the approved persistent listener configuration."
    "${network_reapply_helper}"
fi

# Remove only exact known leftovers; never recursively delete an administrator's
# whole systemd drop-in directory.
systemctl disable --now "${LEGACY_SERVICES[@]}" >/dev/null 2>&1 || true
for path in "${LEGACY_UNIT_FILES[@]}"; do rm -f -- "${path}"; done
rm -f -- \
    /etc/systemd/system/printproxy.service.d/paths.conf \
    /etc/systemd/system/commercialrchproxy.service.d/10-secondary-ip.conf \
    /etc/systemd/system/commercialrchproxy-dumper.service.d/10-secondary-ip.conf
rmdir --ignore-fail-on-non-empty \
    /etc/systemd/system/printproxy.service.d \
    /etc/systemd/system/commercialrchproxy.service.d \
    /etc/systemd/system/commercialrchproxy-dumper.service.d 2>/dev/null || true
systemctl daemon-reload
systemctl reset-failed "${LEGACY_SERVICES[@]}" >/dev/null 2>&1 || true

for service in "${LEGACY_SERVICES[@]}"; do
    systemctl is-active --quiet "${service}" 2>/dev/null && \
        die "legacy service remains active: ${service}"
done
[[ ! -e /opt/printproxy && ! -e /opt/commercialrchproxy ]] || \
    die "legacy runtime remains under /opt"
if command -v nft >/dev/null 2>&1 && \
   nft list table inet printproxy_filter >/dev/null 2>&1; then
    die "legacy nftables table inet printproxy_filter still exists"
fi
for listener in "${expected_listeners[@]}"; do
    ip -o -4 address show | awk -v wanted="${listener}" '
        $4 ~ ("^" wanted "/") { found=1 }
        END { exit !found }
    ' || die "listener ${listener} disappeared; reapply the persistent network configuration before installation"
done

trap - EXIT INT TERM HUP
note "Legacy executable installations removed; evidence/configuration/accounts preserved."
note "Verified recovery archive: ${archive_path}"
note "Next: validate the RetailPrintGuard site YAML and run install.sh."
