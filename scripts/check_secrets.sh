#!/usr/bin/env bash
# High-confidence secret and private-evidence scan for tracked and untracked candidates.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
command -v git >/dev/null || { printf 'ERROR: git not found\n' >&2; exit 69; }
if [[ -n "${PYTHON:-}" ]]; then
    python_bin="${PYTHON}"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    python_bin=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys' >/dev/null 2>&1; then
    python_bin=python
else
    printf 'ERROR: working Python 3 interpreter not found\n' >&2
    exit 69
fi

cd -- "${REPO_ROOT}"
git rev-parse --is-inside-work-tree >/dev/null

"${python_bin}" - <<'PY'
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

root = pathlib.Path.cwd()
listed = subprocess.run(
    ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    check=True,
    capture_output=True,
).stdout.split(b"\0")
paths = [pathlib.Path(raw.decode("utf-8", "surrogateescape")) for raw in listed if raw]

forbidden_suffixes = {
    ".jfif", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic",
    ".pdf", ".pcap", ".pcapng", ".raw", ".sqlite", ".sqlite3", ".db", ".sql", ".dump", ".dmp",
    ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst", ".zip", ".7z", ".rar", ".bak",
    ".key", ".pem", ".crt", ".cer", ".p12", ".pfx",
}
content_patterns = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github-token", re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("openai-key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-jwt", re.compile(rb"Bearer\s+[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", re.I)),
)
credentialed_url = re.compile(
    rb"(?:mysql(?:\+pymysql)?|mariadb|postgres(?:ql)?|https?)://[^\s/:@]+:([^\s/@]+)@",
    re.I,
)
private_ipv4 = re.compile(
    rb"(?<![0-9])(?:10(?:\.[0-9]{1,3}){3}|192\.168(?:\.[0-9]{1,3}){2}|"
    rb"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2})(?![0-9])"
)
placeholder_passwords = {
    b"CHANGE_ME", b"PASSWORD", b"SECRET", b"SEGRETO", b"SYNTHETIC", b"SUPER-SECRET"
}

findings: list[str] = []
for relative in paths:
    if relative.suffix.lower() in forbidden_suffixes:
        findings.append(f"private artifact extension: {relative.as_posix()}")
        continue
    path = root / relative
    try:
        if not path.is_file():
            continue
        if path.stat().st_size > 5_000_000:
            findings.append(f"candidate exceeds inspection limit: {relative.as_posix()}")
            continue
        data = path.read_bytes()
    except OSError as exc:
        findings.append(f"cannot inspect {relative.as_posix()}: {exc}")
        continue
    if b"\0" in data:
        findings.append(f"unexpected binary candidate: {relative.as_posix()}")
        continue
    for label, pattern in content_patterns:
        if pattern.search(data):
            findings.append(f"{label}: {relative.as_posix()}")
    for match in credentialed_url.finditer(data):
        password = match.group(1).upper()
        if password in placeholder_passwords or b"${" in password or b"<" in password:
            continue
        findings.append(f"credentialed-url: {relative.as_posix()}")
    if private_ipv4.search(data):
        findings.append(f"private-ipv4: {relative.as_posix()}")

    if relative.suffix.lower() in {".env", ".yaml", ".yml", ".toml", ".ini"}:
        for number, line in enumerate(data.splitlines(), 1):
            match = re.search(
                rb"(?i)\b(password|passwd|token|secret)\b\s*[:=]\s*[\"']?([^\s\"'#]{8,})",
                line,
            )
            if not match:
                continue
            value = match.group(2).upper()
            if any(marker in value for marker in (b"CHANGE_ME", b"EXAMPLE", b"PLACEHOLDER", b"SYNTHETIC")):
                continue
            findings.append(f"credential assignment: {relative.as_posix()}:{number}")

if findings:
    print("FAIL: possible secrets/private evidence found:", file=sys.stderr)
    for finding in sorted(set(findings)):
        print(f"  {finding}", file=sys.stderr)
    raise SystemExit(1)

print(f"PASS: inspected {len(paths)} candidate files; no high-confidence secret/private artifact found")
PY
