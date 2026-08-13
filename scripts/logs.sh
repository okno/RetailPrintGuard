#!/usr/bin/env bash
# Unified journald view for all application services; no payload files are read.

set -Eeuo pipefail
follow=no
since="-30 minutes"
while (( $# > 0 )); do
    case "$1" in
        --follow) follow=yes; shift ;;
        --since)
            (( $# >= 2 )) || { printf 'ERROR: --since requires a value\n' >&2; exit 2; }
            since="$2"; shift 2
            ;;
        --help|-h)
            printf 'Usage: %s [--follow] [--since "-30 minutes"]\n' "$0"
            exit 0
            ;;
        *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done

units=(
    -u retailprintguard-pos-proxy.service
    -u retailprintguard-rch-proxy.service
    -u retailprintguard-ingestion.service
    -u retailprintguard-parser.service
    -u retailprintguard-correlation.service
    -u retailprintguard-fraud.service
    -u retailprintguard-api.service
)
if [[ "${follow}" == yes ]]; then
    exec journalctl "${units[@]}" --since "${since}" --follow --no-pager
fi
exec journalctl "${units[@]}" --since "${since}" --no-pager
