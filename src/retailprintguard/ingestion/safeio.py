"""Bounded no-follow reads used by every source adapter."""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any

from retailprintguard.ingestion.errors import SourceBusyError, SourceValidationError


def validate_root(root: Path) -> Path:
    root = Path(root)
    try:
        info = root.lstat()
    except OSError as exc:
        raise SourceValidationError(f"source root is unavailable: {root}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SourceValidationError(f"source root must be a non-symlink directory: {root}")
    return root.resolve(strict=True)


def contained_path(root: Path, path: Path) -> Path:
    root_resolved = validate_root(root)
    candidate = Path(path)
    try:
        relative = candidate.absolute().relative_to(Path(root).absolute())
    except ValueError as exc:
        raise SourceValidationError(f"path is outside source root: {candidate}") from exc
    current = Path(root).absolute()
    for part in relative.parts:
        current /= part
        try:
            if current.is_symlink():
                raise SourceValidationError(f"symlink is forbidden in source path: {current}")
        except OSError as exc:
            raise SourceValidationError(f"cannot inspect source path: {current}: {exc}") from exc
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise SourceValidationError(f"source path escapes or is missing: {candidate}") from exc
    return resolved


def safe_child(root: Path, parent: Path, name: object) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name or "\x00" in name:
        raise SourceValidationError(f"unsafe artifact name: {name!r}")
    parent_resolved = contained_path(root, parent)
    path = contained_path(root, parent / name)
    if path.parent != parent_resolved:
        raise SourceValidationError(f"artifact escapes its source directory: {name!r}")
    return path


def read_regular_file(root: Path, path: Path, *, max_bytes: int) -> bytes:
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    resolved = contained_path(root, path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        raise SourceValidationError(f"cannot open source artifact: {resolved}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SourceValidationError(f"source artifact is not a regular file: {resolved}")
        if before.st_size > max_bytes:
            raise SourceValidationError(
                f"source artifact exceeds {max_bytes} bytes: {resolved} ({before.st_size})"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise SourceValidationError(
                    f"source artifact grew beyond {max_bytes} bytes while reading: {resolved}"
                )
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SourceBusyError(f"source artifact changed while reading: {resolved}")
        return b"".join(chunks)
    except OSError as exc:
        raise SourceBusyError(
            f"I/O error while snapshotting source artifact: {resolved}: {exc}"
        ) from exc
    finally:
        with suppress(OSError):
            os.close(fd)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_bytes(data: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SourceValidationError(f"invalid JSON in {label}: {exc}") from exc


def read_json_object(
    root: Path, path: Path, *, max_bytes: int, label: str
) -> tuple[bytes, dict[str, Any]]:
    raw = read_regular_file(root, path, max_bytes=max_bytes)
    value = parse_json_bytes(raw, label=label)
    if not isinstance(value, dict):
        raise SourceValidationError(f"{label} must contain one JSON object")
    return raw, value


def iter_files_no_symlinks(root: Path, filename: str, *, maximum: int) -> tuple[Path, ...]:
    if maximum < 1:
        raise ValueError("maximum must be positive")
    root_resolved = validate_root(root)
    found: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise SourceValidationError(f"cannot traverse source tree: {error}") from error

    for base, directories, files in os.walk(root_resolved, followlinks=False, onerror=walk_error):
        base_path = Path(base)
        safe_directories: list[str] = []
        for name in directories:
            child = base_path / name
            if not child.is_symlink():
                safe_directories.append(name)
        directories[:] = safe_directories
        if filename not in files:
            continue
        candidate = base_path / filename
        if candidate.is_symlink():
            continue
        found.append(candidate)
        if len(found) > maximum:
            raise SourceValidationError(f"discovery exceeds the {maximum}-candidate limit")
    return tuple(sorted(found))
