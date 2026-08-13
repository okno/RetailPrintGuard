#!/usr/bin/env bash
# Build an argv array only; no environment value is evaluated as shell code.

set -Eeuo pipefail
IFS=$'\n\t'

readonly RPG_BIN="/opt/retailprintguard/current/.venv/bin/retailprintguard-ingestion"
readonly RPG_CONFIG="${RPG_CONFIG:-/etc/retailprintguard/config.yaml}"
readonly RPG_CANONICAL_SPOOL_ROOT="${RPG_CANONICAL_SPOOL_ROOT:-/var/lib/retailprintguard/spool}"

[[ -x "${RPG_BIN}" ]] || { printf 'ERROR: missing ingestion executable\n' >&2; exit 78; }

args=(
    --config "${RPG_CONFIG}"
    --repository-factory retailprintguard.db.repository:create_ingestion_repository
    --json-logs
)

help_text="$(${RPG_BIN} --help 2>&1)"
source_count=0
if grep -q -- '--canonical-root' <<<"${help_text}"; then
    args+=(--canonical-root "${RPG_CANONICAL_SPOOL_ROOT}")
    source_count=$((source_count + 1))
fi
if [[ -n "${RPG_RCH_LEGACY_ROOT:-}" ]]; then
    args+=(--rch-root "${RPG_RCH_LEGACY_ROOT}")
    source_count=$((source_count + 1))
fi
if [[ -n "${RPG_PRINTPROXY_LEGACY_ROOT:-}" ]]; then
    args+=(--printproxy-root "${RPG_PRINTPROXY_LEGACY_ROOT}")
    source_count=$((source_count + 1))
    if [[ -n "${RPG_PRINTPROXY_HMAC_KEY_FILE:-}" ]]; then
        args+=(--printproxy-hmac-key-file "${RPG_PRINTPROXY_HMAC_KEY_FILE}")
    fi
fi

if (( source_count == 0 )); then
    printf '%s\n' \
        'ERROR: installed ingestion CLI has no --canonical-root adapter and no legacy source is configured.' >&2
    exit 78
fi

exec "${RPG_BIN}" "${args[@]}"
