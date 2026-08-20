"""Read-only early beeper signal from bounded unpublished POS spool prefixes.

This watcher is an operational convenience only. An unpublished ``*.partial``
directory is never treated as validated evidence and is never persisted as a
parsed result. Authoritative ingestion continues to require ``.ready`` and the
manifest/hash checks in the canonical adapter.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from retailprintguard.parser.beeper import is_complete_pos_command

LOGGER = logging.getLogger("retailprintguard.parser.spool_beeper")
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_DEVICE_ENTRIES = 512


class PosCommandBeeper(Protocol):
    def enqueue(self, device_id: str, *, event_id: str | None = None) -> bool: ...


def _read_growing_regular_file(path: Path, *, maximum: int) -> bytes | None:
    """Read one bounded prefix through O_NOFOLLOW while the writer may append."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= maximum:
            return None
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return None
        payload = b"".join(chunks)
        return payload if len(payload) == before.st_size else None
    finally:
        os.close(descriptor)


class PosSpoolBeeperWatcher:
    """Poll selected POS capture prefixes without writing or following links."""

    def __init__(
        self,
        spool_root: Path,
        device_ids: Sequence[str],
        beeper: PosCommandBeeper,
        *,
        poll_seconds: float = 0.1,
        maximum_input_bytes: int = _MAX_INPUT_BYTES,
    ) -> None:
        if not spool_root.is_absolute():
            raise ValueError("beeper spool root must be absolute")
        if not 0.05 <= poll_seconds <= 1:
            raise ValueError("beeper spool poll interval must be between 0.05 and 1 seconds")
        if not 1 <= maximum_input_bytes <= _MAX_INPUT_BYTES:
            raise ValueError("beeper spool input bound is invalid")
        normalized_ids = tuple(dict.fromkeys(device_ids))
        if not normalized_ids:
            raise ValueError("beeper spool watcher requires selected POS devices")
        self._spool_root = spool_root
        self._device_ids = normalized_ids
        self._beeper = beeper
        self._poll_seconds = poll_seconds
        self._maximum_input_bytes = maximum_input_bytes
        self._observed_sizes: dict[Path, int] = {}
        self._notified: set[Path] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rpg-pos-spool-beeper",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self._poll_seconds + 1.0)
        self._thread = None

    def scan_once(self) -> None:
        active: set[Path] = set()
        for device_id in self._device_ids:
            try:
                active.update(self._scan_device(device_id))
            except OSError as exc:
                LOGGER.warning(
                    "early POS spool scan failed; database fallback remains active",
                    extra={
                        "event": "pos_beeper_spool_scan_failed",
                        "device_id": device_id,
                        "error": type(exc).__name__,
                    },
                )
        self._observed_sizes = {
            path: size for path, size in self._observed_sizes.items() if path in active
        }
        self._notified.intersection_update(active)

    def _scan_device(self, device_id: str) -> set[Path]:
        device_root = self._spool_root / device_id
        try:
            root_info = device_root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return set()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            return set()
        candidates: list[Path] = []
        with os.scandir(device_root) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_DEVICE_ENTRIES:
                    break
                try:
                    if (
                        entry.name.endswith(".partial")
                        and not entry.is_symlink()
                        and entry.is_dir(follow_symlinks=False)
                    ):
                        candidates.append(Path(entry.path))
                except OSError:
                    continue
        active = set(candidates)
        for job_path in candidates:
            if job_path in self._notified:
                continue
            raw_path = job_path / "client.raw"
            try:
                size = raw_path.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if self._observed_sizes.get(job_path) == size:
                continue
            self._observed_sizes[job_path] = size
            payload = _read_growing_regular_file(
                raw_path,
                maximum=self._maximum_input_bytes,
            )
            if payload is None or not is_complete_pos_command(payload):
                continue
            event_id = job_path.name.removesuffix(".partial")
            if self._beeper.enqueue(device_id, event_id=event_id):
                self._notified.add(job_path)
                LOGGER.info(
                    "POS command beeper queued from unpublished spool prefix",
                    extra={
                        "event": "pos_beeper_spool_early_queued",
                        "device_id": device_id,
                        "metrics": {"observed_bytes": len(payload)},
                    },
                )
        return active

    def _run(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self._poll_seconds)


__all__ = ["PosSpoolBeeperWatcher"]
