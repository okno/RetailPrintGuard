"""Durable, bidirectional, append-only capture spool.

The network relay submits immutable capture events with ``put_nowait``.  All
filesystem I/O happens on one dedicated writer thread, so a slow disk cannot
silently turn into unbounded memory growth or block an asyncio socket pump.
Only directories carrying a valid ``.ready`` marker are ingestion candidates;
an unclean process exit leaves a recoverable ``*.partial`` directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import queue
import threading
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

SCHEMA_VERSION = 1
RAW_FILE_BY_DIRECTION = {
    "client_to_device": "client.raw",
    "device_to_client": "device.raw",
}


class Direction(StrEnum):
    CLIENT_TO_DEVICE = "client_to_device"
    DEVICE_TO_CLIENT = "device_to_client"


class StorageFailurePolicy(StrEnum):
    CONTINUE = "continue"
    ABORT = "abort"


class CaptureError(RuntimeError):
    """Capture cannot accept or durably store another event."""


class CaptureQueueFull(CaptureError):
    """The bounded capture queue reached its configured capacity."""


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    session_id: str
    job_id: str
    device_id: str
    device_name: str
    device_type: str
    parser: str
    client_endpoint: tuple[str, int]
    listener_endpoint: tuple[str, int]
    target_endpoint: tuple[str, int]
    connected_at_utc: str
    connected_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class CaptureEvent:
    sequence: int
    observed_sequence: int
    direction_sequence: int
    direction: Direction
    kind: str
    captured_at_utc: str
    captured_monotonic_ns: int
    offset: int
    payload: bytes
    forwarded: bool
    forwarded_at_utc: str
    forward_error: str | None = None


@dataclass(frozen=True, slots=True)
class SessionCloseSummary:
    closed_at_utc: str
    closed_monotonic_ns: int
    close_reason: str
    transport_complete: bool
    observed_bytes: dict[str, int]
    observed_chunks: dict[str, int]
    transport_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublishedCapture:
    job_path: Path
    manifest_path: Path
    ready_path: Path
    manifest_sha256: str


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_facts(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return {"name": path.name, "size": size, "sha256": digest.hexdigest()}


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, mode: int = 0o640) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while creating atomic spool file")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _open_capture_file(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o640)
    return os.fdopen(descriptor, "wb", buffering=0)


def _write_all(handle: BinaryIO, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = handle.write(view)
        if written is None or written <= 0:
            raise OSError("short write in capture stream")
        view = view[written:]


def _flush_durable(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


class _SessionSpool:
    def __init__(self, root: Path, descriptor: SessionDescriptor, *, fsync_each_event: bool):
        self.descriptor = descriptor
        self.fsync_each_event = fsync_each_event
        device_root = root / descriptor.device_id
        device_root.mkdir(mode=0o750, parents=True, exist_ok=True)
        if device_root.is_symlink() or not device_root.is_dir():
            raise CaptureError(f"unsafe device spool directory: {device_root}")

        stamp = descriptor.connected_at_utc.replace(":", "").replace("-", "")
        stamp = stamp.replace(".", "").replace("Z", "Z_")
        self.partial_path = device_root / f"{stamp}{descriptor.job_id}.partial"
        self.final_path = self.partial_path.with_name(
            self.partial_path.name.removesuffix(".partial")
        )
        self.partial_path.mkdir(mode=0o750)

        session_document = {
            "schema_version": SCHEMA_VERSION,
            "session": _descriptor_document(descriptor),
        }
        _atomic_write(
            self.partial_path / "session.json",
            _canonical_json_bytes(session_document) + b"\n",
        )
        self.raw_handles = {
            Direction.CLIENT_TO_DEVICE: _open_capture_file(self.partial_path / "client.raw"),
            Direction.DEVICE_TO_CLIENT: _open_capture_file(self.partial_path / "device.raw"),
        }
        self.timeline_handle = _open_capture_file(self.partial_path / "timeline.jsonl")
        self.next_sequence = 0
        self.next_direction_sequence = {direction: 0 for direction in Direction}
        self.captured_bytes = {direction.value: 0 for direction in Direction}
        self.captured_chunks = {direction.value: 0 for direction in Direction}
        self.eof_events = {direction.value: 0 for direction in Direction}
        self.timeline_events = 0
        self.previous_event_sha256: str | None = None
        self.errors: list[str] = []
        self.closed = False

    def append(self, event: CaptureEvent) -> None:
        if self.closed:
            raise CaptureError("capture session is already closed")
        if event.sequence != self.next_sequence:
            raise CaptureError(
                f"timeline sequence mismatch: expected {self.next_sequence}, got {event.sequence}"
            )
        expected_direction_sequence = self.next_direction_sequence[event.direction]
        if event.direction_sequence != expected_direction_sequence:
            raise CaptureError(
                "direction sequence mismatch: "
                f"expected {expected_direction_sequence}, got {event.direction_sequence}"
            )
        expected_offset = self.captured_bytes[event.direction.value]
        if event.offset != expected_offset:
            raise CaptureError(
                f"stream offset mismatch: expected {expected_offset}, got {event.offset}"
            )
        if event.kind not in {"data", "eof"}:
            raise CaptureError(f"unsupported capture event kind: {event.kind}")
        if event.kind == "eof" and event.payload:
            raise CaptureError("EOF event cannot carry a payload")

        if event.payload:
            raw_handle = self.raw_handles[event.direction]
            _write_all(raw_handle, event.payload)
            if self.fsync_each_event:
                _flush_durable(raw_handle)

        event_body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.descriptor.session_id,
            "job_id": self.descriptor.job_id,
            "device_id": self.descriptor.device_id,
            "sequence": event.sequence,
            "observed_sequence": event.observed_sequence,
            "direction_sequence": event.direction_sequence,
            "direction": event.direction.value,
            "kind": event.kind,
            "captured_at_utc": event.captured_at_utc,
            "captured_monotonic_ns": event.captured_monotonic_ns,
            "offset": event.offset,
            "length": len(event.payload),
            "payload_sha256": _sha256_bytes(event.payload),
            "forwarded": event.forwarded,
            "forwarded_at_utc": event.forwarded_at_utc,
            "forward_error": event.forward_error,
            "previous_event_sha256": self.previous_event_sha256,
        }
        event_sha256 = _sha256_bytes(_canonical_json_bytes(event_body))
        timeline_entry = {**event_body, "event_sha256": event_sha256}
        timeline_line = _canonical_json_bytes(timeline_entry) + b"\n"
        _write_all(self.timeline_handle, timeline_line)
        if self.fsync_each_event:
            _flush_durable(self.timeline_handle)

        self.previous_event_sha256 = event_sha256
        self.next_sequence += 1
        self.next_direction_sequence[event.direction] += 1
        self.timeline_events += 1
        if event.kind == "data":
            self.captured_bytes[event.direction.value] += len(event.payload)
            self.captured_chunks[event.direction.value] += 1
        else:
            self.eof_events[event.direction.value] += 1

    def mark_error(self, error: str) -> None:
        self.errors.append(error[:1000])

    def finalize(
        self,
        summary: SessionCloseSummary,
        *,
        capture_errors: tuple[str, ...],
        dropped_chunks: int,
        dropped_bytes: int,
    ) -> PublishedCapture:
        self._close_handles()
        all_errors = [*self.errors, *capture_errors, *summary.transport_errors]
        byte_complete = all(
            summary.observed_bytes.get(direction.value, 0)
            == self.captured_bytes[direction.value]
            for direction in Direction
        )
        chunk_complete = all(
            summary.observed_chunks.get(direction.value, 0)
            == self.captured_chunks[direction.value]
            for direction in Direction
        )
        complete = (
            summary.transport_complete
            and byte_complete
            and chunk_complete
            and not all_errors
            and dropped_chunks == 0
        )
        files = {
            name: _file_facts(self.partial_path / name)
            for name in ("client.raw", "device.raw", "timeline.jsonl", "session.json")
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "capture_format": "retailprintguard-bidirectional-v1",
            "session": _descriptor_document(self.descriptor),
            "closed_at_utc": summary.closed_at_utc,
            "closed_monotonic_ns": summary.closed_monotonic_ns,
            "close_reason": summary.close_reason,
            "status": "COMPLETE" if complete else "PARTIAL",
            "transport_complete": summary.transport_complete,
            "storage_complete": (
                byte_complete
                and chunk_complete
                and not self.errors
                and not capture_errors
                and dropped_chunks == 0
            ),
            "observed_bytes": summary.observed_bytes,
            "observed_chunks": summary.observed_chunks,
            "captured_bytes": self.captured_bytes,
            "captured_chunks": self.captured_chunks,
            "eof_events": self.eof_events,
            "timeline_events": self.timeline_events,
            "last_event_sha256": self.previous_event_sha256,
            "dropped_chunks": dropped_chunks,
            "dropped_bytes": dropped_bytes,
            "errors": all_errors,
            "files": files,
        }
        manifest_bytes = _canonical_json_bytes(manifest) + b"\n"
        manifest_sha256 = _sha256_bytes(manifest_bytes)
        _atomic_write(self.partial_path / "manifest.json", manifest_bytes)
        if self.final_path.exists():
            raise CaptureError(f"refusing to replace existing job directory: {self.final_path}")
        os.replace(self.partial_path, self.final_path)
        _fsync_directory(self.final_path.parent)
        ready_path = _publish_ready(self.final_path, self.descriptor.job_id, manifest_sha256)
        return PublishedCapture(
            job_path=self.final_path,
            manifest_path=self.final_path / "manifest.json",
            ready_path=ready_path,
            manifest_sha256=manifest_sha256,
        )

    def abandon(self) -> Path:
        """Close descriptors but leave the unpublished directory for recovery."""
        self._close_handles()
        return self.partial_path

    def _close_handles(self) -> None:
        if self.closed:
            return
        first_error: BaseException | None = None
        for handle in [*self.raw_handles.values(), self.timeline_handle]:
            try:
                _flush_durable(handle)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            try:
                handle.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self.closed = True
        if first_error is not None:
            raise first_error


def _descriptor_document(descriptor: SessionDescriptor) -> dict[str, Any]:
    document = asdict(descriptor)
    for key in ("client_endpoint", "listener_endpoint", "target_endpoint"):
        host, port = document[key]
        document[key] = {"host": host, "port": port}
    return document


def _publish_ready(job_path: Path, job_id: str, manifest_sha256: str) -> Path:
    ready = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "manifest_sha256": manifest_sha256,
    }
    ready_path = job_path / ".ready"
    if not ready_path.exists():
        _atomic_write(ready_path, _canonical_json_bytes(ready) + b"\n")
    return ready_path


@dataclass(slots=True)
class _OpenCommand:
    descriptor: SessionDescriptor
    future: Future[None] | None


@dataclass(slots=True)
class _EventCommand:
    session_id: str
    event: CaptureEvent


@dataclass(slots=True)
class _CloseCommand:
    session_id: str
    summary: SessionCloseSummary
    capture_errors: tuple[str, ...]
    dropped_chunks: int
    dropped_bytes: int
    future: Future[PublishedCapture]


@dataclass(slots=True)
class _StopCommand:
    future: Future[None]


class CaptureSession:
    def __init__(
        self,
        manager: CaptureManager | None,
        descriptor: SessionDescriptor,
        policy: StorageFailurePolicy,
        *,
        initial_error: str | None = None,
    ) -> None:
        self.manager = manager
        self.descriptor = descriptor
        self.policy = policy
        self._accepting = manager is not None and initial_error is None
        self._sequence = 0
        self._errors: list[str] = []
        self._dropped_chunks = 0
        self._dropped_bytes = 0
        self.failure_event = asyncio.Event()
        if initial_error is not None:
            self._mark_failure(initial_error)

    @property
    def failed(self) -> bool:
        return bool(self._errors)

    @property
    def failure_message(self) -> str:
        return self._errors[-1] if self._errors else "capture failed"

    def record(
        self,
        *,
        direction: Direction,
        direction_sequence: int,
        kind: str,
        captured_at_utc: str,
        captured_monotonic_ns: int,
        offset: int,
        payload: bytes,
        forwarded: bool,
        forwarded_at_utc: str,
        forward_error: str | None,
        observed_sequence: int | None = None,
    ) -> bool:
        if not self._accepting or self.manager is None:
            if payload:
                self._dropped_chunks += 1
                self._dropped_bytes += len(payload)
            return False
        event = CaptureEvent(
            sequence=self._sequence,
            observed_sequence=(
                self._sequence if observed_sequence is None else observed_sequence
            ),
            direction_sequence=direction_sequence,
            direction=direction,
            kind=kind,
            captured_at_utc=captured_at_utc,
            captured_monotonic_ns=captured_monotonic_ns,
            offset=offset,
            payload=payload,
            forwarded=forwarded,
            forwarded_at_utc=forwarded_at_utc,
            forward_error=forward_error,
        )
        try:
            self.manager._put_event(_EventCommand(self.descriptor.session_id, event))
        except queue.Full:
            if payload:
                self._dropped_chunks += 1
                self._dropped_bytes += len(payload)
            self._accepting = False
            self._mark_failure("bounded capture queue is full")
            return False
        self._sequence += 1
        return True

    async def wait_failed(self) -> str:
        await self.failure_event.wait()
        return self.failure_message

    async def finalize(self, summary: SessionCloseSummary) -> PublishedCapture | None:
        if self.manager is None:
            return None
        return await self.manager._close_session(
            self,
            summary,
            tuple(self._errors),
            self._dropped_chunks,
            self._dropped_bytes,
        )

    def _mark_failure(self, error: str) -> None:
        normalized = error[:1000]
        if normalized not in self._errors:
            self._errors.append(normalized)
        self._accepting = False
        self.failure_event.set()


class CaptureManager:
    """Own the bounded queue and its dedicated filesystem writer thread."""

    def __init__(
        self,
        root: Path,
        *,
        queue_max_events: int = 4096,
        fsync_each_event: bool = True,
        recover_device_ids: frozenset[str] | None = None,
    ) -> None:
        if queue_max_events < 1:
            raise ValueError("queue_max_events must be positive")
        self.root = root
        self.fsync_each_event = fsync_each_event
        self.recover_device_ids = recover_device_ids
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_max_events)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, CaptureSession] = {}
        self._running = False

    async def start(self) -> list[Path]:
        if self._running:
            return []
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise CaptureError(f"unsafe spool root: {self.root}")
        recovered = await asyncio.to_thread(
            recover_incomplete_spool, self.root, self.recover_device_ids
        )
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._thread = threading.Thread(
            target=self._worker,
            name="retailprintguard-capture",
            daemon=True,
        )
        self._thread.start()
        return recovered

    async def open_session(
        self,
        descriptor: SessionDescriptor,
        policy: StorageFailurePolicy,
    ) -> CaptureSession:
        if not self._running:
            raise CaptureError("capture manager is not running")
        if descriptor.session_id in self._sessions:
            raise CaptureError(f"duplicate session id: {descriptor.session_id}")
        session = CaptureSession(self, descriptor, policy)
        self._sessions[descriptor.session_id] = session
        future: Future[None] = Future()
        try:
            await self._put_control(_OpenCommand(descriptor, future))
            await asyncio.wrap_future(future)
        except BaseException:
            self._sessions.pop(descriptor.session_id, None)
            raise
        return session

    def open_session_nowait(
        self,
        descriptor: SessionDescriptor,
        policy: StorageFailurePolicy,
    ) -> CaptureSession:
        """Queue session initialization without putting disk I/O on the relay path.

        FIFO ordering guarantees that the writer opens the files before it sees
        capture events.  Capacity is bounded: if even the open command cannot
        be queued, the caller receives an explicit failure immediately and can
        apply the configured storage policy without waiting on the filesystem.
        """

        if not self._running:
            raise CaptureError("capture manager is not running")
        if descriptor.session_id in self._sessions:
            raise CaptureError(f"duplicate session id: {descriptor.session_id}")
        session = CaptureSession(self, descriptor, policy)
        self._sessions[descriptor.session_id] = session
        try:
            self._queue.put_nowait(_OpenCommand(descriptor, None))
        except queue.Full as exc:
            self._sessions.pop(descriptor.session_id, None)
            session._mark_failure("bounded capture queue is full before session open")
            raise CaptureQueueFull(
                "bounded capture queue is full before session open"
            ) from exc
        return session

    def disabled_session(
        self,
        descriptor: SessionDescriptor,
        policy: StorageFailurePolicy,
        error: str,
    ) -> CaptureSession:
        return CaptureSession(None, descriptor, policy, initial_error=error)

    async def stop(self) -> None:
        if not self._running:
            return
        future: Future[None] = Future()
        await self._put_control(_StopCommand(future))
        await asyncio.wrap_future(future)
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 10)
        self._thread = None
        self._running = False
        self._sessions.clear()

    def _put_event(self, command: _EventCommand) -> None:
        if not self._running:
            raise queue.Full
        self._queue.put_nowait(command)

    async def _put_control(self, command: object) -> None:
        if not self._running and not isinstance(command, _StopCommand):
            raise CaptureError("capture manager is not running")
        try:
            await asyncio.to_thread(self._queue.put, command, True, 10)
        except queue.Full as exc:
            raise CaptureQueueFull("capture control queue remained full") from exc

    async def _close_session(
        self,
        session: CaptureSession,
        summary: SessionCloseSummary,
        errors: tuple[str, ...],
        dropped_chunks: int,
        dropped_bytes: int,
    ) -> PublishedCapture:
        future: Future[PublishedCapture] = Future()
        command = _CloseCommand(
            session_id=session.descriptor.session_id,
            summary=summary,
            capture_errors=errors,
            dropped_chunks=dropped_chunks,
            dropped_bytes=dropped_bytes,
            future=future,
        )
        try:
            await self._put_control(command)
            return await asyncio.wrap_future(future)
        finally:
            self._sessions.pop(session.descriptor.session_id, None)

    def _notify_failure(self, session_id: str, error: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session._mark_failure(error)

    def _worker(self) -> None:
        writers: dict[str, _SessionSpool] = {}
        while True:
            command = self._queue.get()
            try:
                if isinstance(command, _OpenCommand):
                    try:
                        writers[command.descriptor.session_id] = _SessionSpool(
                            self.root,
                            command.descriptor,
                            fsync_each_event=self.fsync_each_event,
                        )
                    except BaseException as exc:
                        self._threadsafe_failure(
                            command.descriptor.session_id,
                            f"capture open failed: {type(exc).__name__}: {exc}",
                        )
                        if command.future is not None:
                            _future_exception(command.future, exc)
                    else:
                        if command.future is not None:
                            _future_result(command.future, None)
                elif isinstance(command, _EventCommand):
                    writer = writers.get(command.session_id)
                    if writer is None:
                        self._threadsafe_failure(
                            command.session_id, "capture writer is unavailable"
                        )
                        continue
                    try:
                        writer.append(command.event)
                    except BaseException as exc:
                        message = f"capture write failed: {type(exc).__name__}: {exc}"
                        writer.mark_error(message)
                        self._threadsafe_failure(command.session_id, message)
                elif isinstance(command, _CloseCommand):
                    writer = writers.pop(command.session_id, None)
                    if writer is None:
                        _future_exception(
                            command.future, CaptureError("capture writer is unavailable")
                        )
                        continue
                    try:
                        published = writer.finalize(
                            command.summary,
                            capture_errors=command.capture_errors,
                            dropped_chunks=command.dropped_chunks,
                            dropped_bytes=command.dropped_bytes,
                        )
                    except BaseException as exc:
                        writer.abandon()
                        _future_exception(command.future, exc)
                    else:
                        _future_result(command.future, published)
                elif isinstance(command, _StopCommand):
                    for writer in writers.values():
                        with contextlib.suppress(Exception):
                            writer.abandon()
                    writers.clear()
                    _future_result(command.future, None)
                    return
            finally:
                self._queue.task_done()

    def _threadsafe_failure(self, session_id: str, error: str) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._notify_failure, session_id, error)


def _future_result(future: Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _future_exception(future: Future[Any], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


def recover_incomplete_spool(
    root: Path, device_ids: frozenset[str] | None = None
) -> list[Path]:
    """Publish crash remnants once, preserving every byte and marking uncertainty."""
    recovered: list[Path] = []
    if not root.exists():
        return recovered
    for device_root in sorted(root.iterdir()):
        if device_root.is_symlink() or not device_root.is_dir():
            continue
        if device_ids is not None and device_root.name not in device_ids:
            continue
        candidates = sorted(path for path in device_root.iterdir() if path.is_dir())
        for candidate in candidates:
            ready_path = candidate / ".ready"
            if ready_path.exists():
                continue
            is_partial = candidate.name.endswith(".partial")
            manifest_path = candidate / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest_bytes = manifest_path.read_bytes()
                    manifest = json.loads(manifest_bytes)
                    job_id = str(manifest["session"]["job_id"])
                except (OSError, ValueError, KeyError, TypeError):
                    preserved = candidate / "manifest.untrusted.json"
                    if preserved.exists():
                        preserved = candidate / f"manifest.untrusted.{uuid4().hex}.json"
                    os.replace(manifest_path, preserved)
                else:
                    final_path = _finish_recovered_directory(candidate, is_partial)
                    _publish_ready(final_path, job_id, _sha256_bytes(manifest_bytes))
                    recovered.append(final_path)
                    continue

            final_path = candidate
            session = _read_recovery_session(candidate, device_root.name)
            job_id = str(session["job_id"])
            files: dict[str, Any] = {}
            for name in ("client.raw", "device.raw", "timeline.jsonl", "session.json"):
                path = candidate / name
                if not path.exists():
                    _atomic_write(path, b"")
                files[name] = _file_facts(path)
            for preserved in sorted(candidate.glob("manifest.untrusted*.json")):
                files[preserved.name] = _file_facts(preserved)
            captured_bytes = {
                Direction.CLIENT_TO_DEVICE.value: files["client.raw"]["size"],
                Direction.DEVICE_TO_CLIENT.value: files["device.raw"]["size"],
            }
            timeline = _inspect_timeline(candidate / "timeline.jsonl")
            timeline_covers_raw = (
                timeline["integrity_ok"]
                and timeline["covered_bytes"] == captured_bytes
            )
            recovery_errors = [
                "unclean shutdown; completeness beyond captured prefixes is unknown"
            ]
            if not timeline_covers_raw:
                recovery_errors.append(
                    "timeline does not cover directional RAW; trailing bytes have no chunk index"
                )
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "capture_format": "retailprintguard-bidirectional-v1",
                "session": session,
                "closed_at_utc": utc_now_text(),
                "closed_monotonic_ns": 0,
                "close_reason": "recovered_after_unclean_shutdown",
                "status": "PARTIAL",
                "transport_complete": False,
                "storage_complete": timeline_covers_raw,
                "observed_bytes": captured_bytes,
                "observed_chunks": timeline["data_chunks"],
                "captured_bytes": captured_bytes,
                "captured_chunks": timeline["data_chunks"],
                "timeline_covered_bytes": timeline["covered_bytes"],
                "eof_events": timeline["eof_events"],
                "timeline_events": timeline["events"],
                "last_event_sha256": timeline["last_event_sha256"],
                "dropped_chunks": 0,
                "dropped_bytes": 0,
                "errors": recovery_errors,
                "files": files,
            }
            manifest_bytes = _canonical_json_bytes(manifest) + b"\n"
            _atomic_write(candidate / "manifest.json", manifest_bytes)
            final_path = _finish_recovered_directory(candidate, is_partial)
            _publish_ready(final_path, job_id, _sha256_bytes(manifest_bytes))
            recovered.append(final_path)
    return recovered


def _finish_recovered_directory(candidate: Path, is_partial: bool) -> Path:
    if not is_partial:
        return candidate
    final_path = candidate.with_name(candidate.name.removesuffix(".partial"))
    if final_path.exists():
        raise CaptureError(f"recovery destination already exists: {final_path}")
    os.replace(candidate, final_path)
    _fsync_directory(final_path.parent)
    return final_path


def _read_recovery_session(path: Path, device_id: str) -> dict[str, Any]:
    try:
        loaded = json.loads((path / "session.json").read_text(encoding="utf-8"))
        session = loaded["session"]
        if not isinstance(session, dict):
            raise TypeError
        return session
    except (OSError, ValueError, KeyError, TypeError):
        stem = path.name.removesuffix(".partial")
        guessed_job_id = stem.rsplit("_", maxsplit=1)[-1]
        return {
            "session_id": guessed_job_id,
            "job_id": guessed_job_id,
            "device_id": device_id,
            "device_name": device_id,
            "device_type": "unknown",
            "parser": "unknown",
            "client_endpoint": {"host": "unknown", "port": 0},
            "listener_endpoint": {"host": "unknown", "port": 0},
            "target_endpoint": {"host": "unknown", "port": 0},
            "connected_at_utc": utc_now_text(),
            "connected_monotonic_ns": 0,
        }


def _inspect_timeline(path: Path) -> dict[str, Any]:
    events = 0
    data_chunks = {direction.value: 0 for direction in Direction}
    eof_events = {direction.value: 0 for direction in Direction}
    covered_bytes = {direction.value: 0 for direction in Direction}
    direction_sequences = {direction.value: 0 for direction in Direction}
    observed_sequences: set[int] = set()
    previous_hash: str | None = None
    integrity_ok = True
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    entry = json.loads(raw_line)
                    event_hash = entry.pop("event_sha256")
                    direction = str(entry["direction"])
                    kind = str(entry["kind"])
                    sequence = int(entry["sequence"])
                    observed_sequence = int(entry["observed_sequence"])
                    direction_sequence = int(entry["direction_sequence"])
                    offset = int(entry["offset"])
                    length = int(entry["length"])
                    if direction not in covered_bytes:
                        raise ValueError
                    if sequence != events or observed_sequence < 0:
                        raise ValueError
                    if observed_sequence in observed_sequences:
                        raise ValueError
                    if direction_sequence != direction_sequences[direction]:
                        raise ValueError
                    if offset != covered_bytes[direction] or length < 0:
                        raise ValueError
                    if entry.get("previous_event_sha256") != previous_hash:
                        raise ValueError
                    if _sha256_bytes(_canonical_json_bytes(entry)) != event_hash:
                        raise ValueError
                    if kind == "data":
                        if length < 1:
                            raise ValueError
                        data_chunks[direction] += 1
                        covered_bytes[direction] += length
                    elif kind == "eof":
                        if length != 0:
                            raise ValueError
                        eof_events[direction] += 1
                    else:
                        raise ValueError
                    direction_sequences[direction] += 1
                    observed_sequences.add(observed_sequence)
                    previous_hash = event_hash
                    events += 1
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    integrity_ok = False
                    break
    except OSError:
        integrity_ok = False
    return {
        "events": events,
        "data_chunks": data_chunks,
        "eof_events": eof_events,
        "covered_bytes": covered_bytes,
        "last_event_sha256": previous_hash,
        "integrity_ok": integrity_ok,
    }


__all__ = [
    "CaptureError",
    "CaptureManager",
    "CaptureQueueFull",
    "CaptureSession",
    "Direction",
    "PublishedCapture",
    "SessionCloseSummary",
    "SessionDescriptor",
    "StorageFailurePolicy",
    "recover_incomplete_spool",
    "utc_now_text",
]
