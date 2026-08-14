#!/usr/bin/env bash
# Run the repository gates without opening proxy listeners or device sockets.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

run_python=yes
run_frontend=yes
while (( $# > 0 )); do
    case "$1" in
        --python-only) run_frontend=no; shift ;;
        --frontend-only) run_python=no; shift ;;
        --help|-h)
            printf 'Usage: %s [--python-only|--frontend-only]\n' "$0"
            exit 0
            ;;
        *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; exit 64 ;;
    esac
done

if [[ "${run_python}" == yes ]]; then
    if [[ -n "${PYTHON:-}" ]]; then
        python_bin="${PYTHON}"
    elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        python_bin="${REPO_ROOT}/.venv/bin/python"
    elif [[ -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
        python_bin="${REPO_ROOT}/.venv/Scripts/python.exe"
    else
        python_bin="$(command -v python3 || command -v python || true)"
    fi
    [[ -n "${python_bin}" ]] || { printf 'ERROR: Python not found\n' >&2; exit 69; }
    (
        cd -- "${REPO_ROOT}"
        "${python_bin}" -m ruff check src tests migrations scripts
        "${python_bin}" -m pytest -q
    )
fi

if [[ "${run_frontend}" == yes ]]; then
    command -v pnpm >/dev/null 2>&1 || {
        printf 'ERROR: pnpm not found; use --python-only only for a documented partial gate\n' >&2
        exit 69
    }
    (
        cd -- "${REPO_ROOT}/frontend"
        pnpm lint
        pnpm test
        pnpm build
    )
fi

printf 'PASS: requested offline repository gates completed\n'
