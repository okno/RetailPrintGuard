#!/usr/bin/env bash
# Download one authorized document artifact and verify the API checksum atomically.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

api_base="${RPG_API_BASE:-http://127.0.0.1:8080/api/v1}"
token_file=""
document_id=""
format="json"
direction="request"
output=""

usage() {
    cat <<'EOF'
Usage: export_document.sh --document-id UUID [options]
  --format raw|txt|json|pdf
  --direction request|response     (raw only)
  --output FILE
  --api-base URL
  --token-file FILE

Set RPG_API_TOKEN or pass a protected token file. The token is never printed.
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --document-id) (( $# >= 2 )) || { usage >&2; exit 64; }; document_id="$2"; shift 2 ;;
        --format) (( $# >= 2 )) || { usage >&2; exit 64; }; format="$2"; shift 2 ;;
        --direction) (( $# >= 2 )) || { usage >&2; exit 64; }; direction="$2"; shift 2 ;;
        --output) (( $# >= 2 )) || { usage >&2; exit 64; }; output="$2"; shift 2 ;;
        --api-base) (( $# >= 2 )) || { usage >&2; exit 64; }; api_base="$2"; shift 2 ;;
        --token-file) (( $# >= 2 )) || { usage >&2; exit 64; }; token_file="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 64 ;;
    esac
done

[[ "${document_id}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$ ]] || {
    printf 'ERROR: invalid document UUID\n' >&2; exit 64;
}
[[ "${format}" =~ ^(raw|txt|json|pdf)$ ]] || { printf 'ERROR: invalid format\n' >&2; exit 64; }
[[ "${direction}" =~ ^(request|response)$ ]] || { printf 'ERROR: invalid direction\n' >&2; exit 64; }
[[ "${api_base}" =~ ^https?://[A-Za-z0-9._:\[\]-]+(/[A-Za-z0-9._~/-]*)?$ ]] || {
    printf 'ERROR: unsafe API base URL\n' >&2; exit 64;
}
api_base="${api_base%/}"

if [[ -n "${token_file}" ]]; then
    [[ -f "${token_file}" && ! -L "${token_file}" ]] || { printf 'ERROR: unsafe token file\n' >&2; exit 66; }
    command -v stat >/dev/null || { printf 'ERROR: stat not found\n' >&2; exit 69; }
    [[ "$(stat -c '%a' "${token_file}")" =~ ^(400|600)$ ]] || {
        printf 'ERROR: token file must be owner-readable only (0400 or 0600)\n' >&2
        exit 77
    }
    token="$(tr -d '\r\n' <"${token_file}")"
else
    token="${RPG_API_TOKEN:-}"
fi
[[ "${token}" =~ ^[A-Za-z0-9._-]{20,4096}$ ]] || { printf 'ERROR: missing or malformed API token\n' >&2; exit 77; }

if [[ -z "${output}" ]]; then
    output="${document_id}.${format}"
fi
output_parent="$(dirname -- "${output}")"
[[ -d "${output_parent}" ]] || { printf 'ERROR: output parent does not exist\n' >&2; exit 66; }
[[ "${output}" != *$'\n'* && "${output}" != *$'\r'* && \
   "${output}" != *'"'* && "${output}" != *\\* ]] || {
    printf 'ERROR: unsafe output path\n' >&2; exit 64;
}
[[ ! -e "${output}" ]] || { printf 'ERROR: output already exists: %s\n' "${output}" >&2; exit 73; }
tmp="$(mktemp "${output_parent}/.document-export.XXXXXXXX")"
headers="$(mktemp "${output_parent}/.document-headers.XXXXXXXX")"
cleanup() { rm -f -- "${tmp}" "${headers}"; }
trap cleanup EXIT

url="${api_base}/documents/${document_id}/${format}"
[[ "${format}" != raw ]] || url+="?direction=${direction}"
command -v curl >/dev/null || { printf 'ERROR: curl not found\n' >&2; exit 69; }
command -v sha256sum >/dev/null || { printf 'ERROR: sha256sum not found\n' >&2; exit 69; }

# Supplying curl options on stdin keeps the bearer token out of argv/process listings.
{
    printf 'silent\nshow-error\nfail-with-body\nproto = "=http,https"\nmax-redirs = 0\n'
    printf 'connect-timeout = 10\nmax-time = 120\n'
    printf 'header = "Authorization: Bearer %s"\n' "${token}"
    printf 'url = "%s"\n' "${url}"
    printf 'output = "%s"\n' "${tmp}"
    printf 'dump-header = "%s"\n' "${headers}"
} | curl --config -
unset token RPG_API_TOKEN

expected="$(awk -F': ' 'tolower($1) == "x-checksum-sha256" {gsub("\r", "", $2); print tolower($2)}' "${headers}" | tail -n 1)"
actual="$(sha256sum -- "${tmp}" | awk '{print $1}')"
[[ "${expected}" =~ ^[0-9a-f]{64}$ && "${actual}" == "${expected}" ]] || {
    printf 'ERROR: API checksum missing or mismatched; artifact not published\n' >&2
    exit 65
}
chmod 0600 "${tmp}"
mv -- "${tmp}" "${output}"
rm -f -- "${headers}"
trap - EXIT
printf 'PASS: exported %s with verified SHA-256 to %s\n' "${format}" "${output}"
