"""Provision the high-risk job-review confirmation secret without clear text at rest."""

from __future__ import annotations

import argparse
import getpass
import importlib
import os
import tempfile
from pathlib import Path

from retailprintguard.api.auth import PasswordService

DEFAULT_OUTPUT = Path("/etc/retailprintguard/review.env")
DEFAULT_ENV_NAME = "RPG_INCOMPLETE_REVIEW_PASSWORD_HASH"


class ReviewSecretVerifier:
    """Bounded verifier for the optional high-risk action confirmation secret."""

    def __init__(self, encoded: str | None) -> None:
        normalized = None if encoded is None else encoded.strip()
        if normalized and (len(normalized) > 1024 or not normalized.startswith("$argon2id$")):
            raise ValueError("review confirmation secret must be an Argon2id hash")
        self._encoded = normalized or None
        self._passwords = PasswordService()

    @property
    def configured(self) -> bool:
        return self._encoded is not None

    def verify(self, password: str) -> bool:
        if self._encoded is None or not 1 <= len(password) <= 1024:
            return False
        return self._passwords.verify(self._encoded, password)


def _validate_output(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    if path != DEFAULT_OUTPUT:
        raise ValueError(f"output path must be {DEFAULT_OUTPUT}")
    if path.is_symlink():
        raise ValueError("refusing a symlink review-secret file")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError("review-secret parent must be a regular directory")


def _prompt_password() -> str:
    first = getpass.getpass("Nuova password di conferma incompleti: ")
    second = getpass.getpass("Ripeti la password: ")
    if first != second:
        raise ValueError("the two passwords do not match")
    if not 14 <= len(first) <= 1024:
        raise ValueError("password length must be between 14 and 1024 characters")
    if "\x00" in first or "\n" in first or "\r" in first:
        raise ValueError("password contains forbidden control characters")
    return first


def provision_review_secret(
    output: Path = DEFAULT_OUTPUT,
    *,
    env_name: str = DEFAULT_ENV_NAME,
    password: str | None = None,
) -> None:
    """Atomically write a root-owned environment file containing only an Argon2 hash."""

    if os.name != "posix" or os.geteuid() != 0:
        raise PermissionError("this command must run as root on the RetailPrintGuard host")
    _validate_output(output)
    secret = _prompt_password() if password is None else password
    if not 14 <= len(secret) <= 1024 or any(char in secret for char in ("\x00", "\n", "\r")):
        raise ValueError("invalid confirmation password")
    encoded = PasswordService().hash(secret)
    group_id = importlib.import_module("grp").getgrnam("retailprintguard-api").gr_gid
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".review.env.", dir=str(output.parent), text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, group_id)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(f"{env_name}={encoded}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Configure the Argon2 confirmation secret for incomplete-job review"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    provision_review_secret(args.output)
    print(
        f"Argon2 review secret written to {args.output}; "
        "restart only retailprintguard-api.service"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
