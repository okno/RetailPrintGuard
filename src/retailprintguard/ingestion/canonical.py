"""Strict adapter for the native RetailPrintGuard bidirectional spool."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from retailprintguard.ingestion.adapters import (
    endpoint,
    parse_datetime,
    require_bool,
    require_int,
    require_optional_string,
    require_sha256,
    require_string,
    require_uuid,
    resolve_device,
    validate_source_instance_id,
)
from retailprintguard.ingestion.dto import (
    ArtifactRole,
    ArtifactSnapshot,
    ImportCandidate,
    NormalizedEnvelope,
    SourceKind,
    StreamChunk,
    StreamDirection,
)
from retailprintguard.ingestion.errors import SourceBusyError, SourceValidationError
from retailprintguard.ingestion.safeio import (
    contained_path,
    parse_json_bytes,
    read_json_object,
    read_regular_file,
    safe_child,
    validate_root,
)

CAPTURE_FORMAT = "retailprintguard-bidirectional-v1"
_MAX_MARKER_BYTES = 65_536
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_SESSION_BYTES = 1_048_576
_MAX_TIMELINE_BYTES = 128 * 1024 * 1024
_MAX_TIMELINE_LINE_BYTES = 65_536
_MAX_TIMELINE_EVENTS = 100_000
_EXPECTED_FILES = ("client.raw", "device.raw", "timeline.jsonl", "session.json")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=True).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceValidationError(f"{label} must be a JSON object")
    return value


def _string_int_map(value: object, label: str) -> dict[str, int]:
    source = _mapping(value, label)
    result: dict[str, int] = {}
    for direction in ("client_to_device", "device_to_client"):
        result[direction] = require_int(source.get(direction), f"{label}.{direction}")
    return result


def _rotating_markers(
    markers: Sequence[Path], *, maximum: int, cursor: int
) -> tuple[tuple[Path, ...], int]:
    if maximum < 1:
        raise ValueError("maximum must be positive")
    if len(markers) <= maximum:
        return tuple(markers), 0
    start = cursor % len(markers)
    rotated = tuple(markers[start:]) + tuple(markers[:start])
    return rotated[:maximum], (start + maximum) % len(markers)


def _file_snapshot(
    root: Path,
    job_dir: Path,
    files: Mapping[str, object],
    name: str,
    *,
    limit: int,
) -> tuple[Path, bytes, str]:
    facts = _mapping(files.get(name), f"manifest.files.{name}")
    if facts.get("name") != name:
        raise SourceValidationError(f"manifest file name mismatch for {name}")
    size = require_int(facts.get("size"), f"manifest.files.{name}.size", maximum=limit)
    digest = require_sha256(facts.get("sha256"), f"manifest.files.{name}.sha256")
    path = safe_child(root, job_dir, name)
    content = read_regular_file(root, path, max_bytes=limit)
    if len(content) != size:
        raise SourceValidationError(f"manifest size mismatch for {name}")
    if not hmac.compare_digest(_digest(content), digest):
        raise SourceValidationError(f"SHA-256 mismatch for {name}")
    return path, content, digest


def _parse_timeline(
    raw: bytes,
    *,
    client_raw: bytes,
    device_raw: bytes,
    session_id: str,
    job_id: str,
    device_id: str,
    manifest: Mapping[str, Any],
    allow_trailing_raw: bool = False,
) -> tuple[StreamChunk, ...]:
    if raw and not raw.endswith(b"\n"):
        raise SourceValidationError("canonical timeline has a truncated final record")
    if len(raw.splitlines()) > _MAX_TIMELINE_EVENTS:
        raise SourceValidationError("canonical timeline exceeds the event limit")

    streams = {
        "client_to_device": client_raw,
        "device_to_client": device_raw,
    }
    direction_map = {
        "client_to_device": StreamDirection.CLIENT_TO_DEVICE,
        "device_to_client": StreamDirection.DEVICE_TO_CLIENT,
    }
    offsets = {name: 0 for name in streams}
    direction_sequences = {name: 0 for name in streams}
    data_counts = {name: 0 for name in streams}
    eof_counts = {name: 0 for name in streams}
    previous_hash: str | None = None
    observed_sequences: set[int] = set()
    events: list[StreamChunk] = []

    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line or len(raw_line) > _MAX_TIMELINE_LINE_BYTES:
            raise SourceValidationError(f"invalid canonical timeline line {line_number}")
        value = parse_json_bytes(raw_line, label=f"canonical timeline line {line_number}")
        event = _mapping(value, f"canonical timeline line {line_number}")
        event_hash = require_sha256(
            event.get("event_sha256"), f"timeline[{line_number}].event_sha256"
        )
        body = dict(event)
        del body["event_sha256"]
        if not hmac.compare_digest(_digest(_canonical_json(body)), event_hash):
            raise SourceValidationError(f"event hash mismatch at timeline line {line_number}")
        if event.get("previous_event_sha256") != previous_hash:
            raise SourceValidationError(f"event hash-chain mismatch at timeline line {line_number}")
        previous_hash = event_hash

        if require_int(event.get("schema_version"), "timeline.schema_version", minimum=1) != 1:
            raise SourceValidationError("unsupported canonical timeline schema")
        if (
            event.get("session_id") != session_id
            or event.get("job_id") != job_id
            or event.get("device_id") != device_id
        ):
            raise SourceValidationError("canonical timeline identity binding mismatch")
        sequence = require_int(event.get("sequence"), "timeline.sequence")
        if sequence != len(events):
            raise SourceValidationError("canonical timeline sequence is not contiguous from zero")
        observed_sequence = require_int(
            event.get("observed_sequence"), "timeline.observed_sequence"
        )
        if observed_sequence in observed_sequences:
            raise SourceValidationError("canonical observed sequence is duplicated")
        observed_sequences.add(observed_sequence)

        direction_name = require_string(event.get("direction"), "timeline.direction", maximum=32)
        if direction_name not in streams:
            raise SourceValidationError(f"unsupported canonical direction: {direction_name!r}")
        direction_sequence = require_int(
            event.get("direction_sequence"), "timeline.direction_sequence"
        )
        if direction_sequence != direction_sequences[direction_name]:
            raise SourceValidationError("canonical direction sequence is not contiguous")
        direction_sequences[direction_name] += 1

        kind = require_string(event.get("kind"), "timeline.kind", maximum=16)
        if kind not in {"data", "eof"}:
            raise SourceValidationError(f"unsupported canonical event kind: {kind!r}")
        offset = require_int(event.get("offset"), "timeline.offset")
        length = require_int(event.get("length"), "timeline.length")
        if offset != offsets[direction_name]:
            raise SourceValidationError("canonical timeline has a directional gap or overlap")
        stream = streams[direction_name]
        if offset + length > len(stream):
            raise SourceValidationError(
                "canonical timeline addresses bytes outside directional RAW"
            )
        if kind == "eof" and length != 0:
            raise SourceValidationError("canonical EOF event carries bytes")
        if kind == "data" and length < 1:
            raise SourceValidationError("canonical data event is empty")
        payload = stream[offset : offset + length]
        payload_hash = require_sha256(
            event.get("payload_sha256"), f"timeline[{line_number}].payload_sha256"
        )
        if not hmac.compare_digest(_digest(payload), payload_hash):
            raise SourceValidationError(f"payload hash mismatch at timeline line {line_number}")
        if kind == "data":
            offsets[direction_name] += length
            data_counts[direction_name] += 1
        else:
            eof_counts[direction_name] += 1

        forwarded = require_bool(event.get("forwarded"), "timeline.forwarded")
        forwarded_at = parse_datetime(event.get("forwarded_at_utc"), "timeline.forwarded_at_utc")
        events.append(
            StreamChunk(
                sequence=sequence,
                direction=direction_map[direction_name],
                received_at=parse_datetime(
                    event.get("captured_at_utc"), "timeline.captured_at_utc"
                ),
                received_unix_ns=None,
                forwarded_unix_ns=None,
                local_write_drain_unix_ns=None,
                monotonic_ns=require_int(
                    event.get("captured_monotonic_ns"), "timeline.captured_monotonic_ns"
                ),
                job_offset=offset,
                session_offset=offset,
                byte_count=length,
                sha256=payload_hash,
                local_write_drain_completed=forwarded,
                forward_status="FORWARDED" if forwarded else "FAILED",
                error=require_optional_string(
                    event.get("forward_error"), "timeline.forward_error", maximum=4096
                ),
                observed_sequence=observed_sequence,
                direction_sequence=direction_sequence,
                event_kind=kind,
                forwarded_at=forwarded_at,
            )
        )

    expected_events = require_int(
        manifest.get("timeline_events"), "manifest.timeline_events", maximum=_MAX_TIMELINE_EVENTS
    )
    if len(events) != expected_events:
        raise SourceValidationError("canonical timeline event count differs from manifest")
    if manifest.get("last_event_sha256") != previous_hash:
        raise SourceValidationError("canonical timeline head differs from manifest")
    captured_bytes = _string_int_map(manifest.get("captured_bytes"), "manifest.captured_bytes")
    raw_bytes = {
        "client_to_device": len(client_raw),
        "device_to_client": len(device_raw),
    }
    if captured_bytes != raw_bytes:
        raise SourceValidationError("captured byte totals differ from directional RAW files")
    if offsets != captured_bytes:
        if not allow_trailing_raw:
            raise SourceValidationError("captured byte totals differ from canonical timeline")
        covered_bytes = _string_int_map(
            manifest.get("timeline_covered_bytes"), "manifest.timeline_covered_bytes"
        )
        if offsets != covered_bytes or any(
            offsets[direction] > captured_bytes[direction] for direction in offsets
        ):
            raise SourceValidationError(
                "partial recovery timeline coverage differs from its manifest"
            )
    if data_counts != _string_int_map(manifest.get("captured_chunks"), "manifest.captured_chunks"):
        raise SourceValidationError("captured chunk totals differ from canonical timeline")
    if eof_counts != _string_int_map(manifest.get("eof_events"), "manifest.eof_events"):
        raise SourceValidationError("EOF totals differ from canonical timeline")
    if manifest.get("status") == "COMPLETE" and observed_sequences != set(range(len(events))):
        raise SourceValidationError(
            "complete canonical timeline observed sequence is not contiguous from zero"
        )
    return tuple(events)


class CanonicalCaptureV1Adapter:
    """Read immutable native capture jobs without writing into their spool."""

    def __init__(
        self,
        root: Path,
        *,
        source_instance_id: str,
        devices_by_target: Mapping[tuple[str, int], str],
        max_payload_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        self.root = validate_root(root)
        self.source_instance_id = validate_source_instance_id(source_instance_id)
        self.devices_by_target = dict(devices_by_target)
        self.max_payload_bytes = max_payload_bytes
        self._discovery_cursor = 0
        self._root_mtime_ns = -1
        self._device_mtimes: dict[Path, int] = {}
        self._markers_by_device: dict[Path, set[Path]] = {}
        self._unready_jobs: set[Path] = set()

    @staticmethod
    def _regular_marker(job_dir: Path) -> Path | None:
        marker = job_dir / ".ready"
        try:
            facts = marker.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SourceValidationError(f"cannot inspect canonical ready marker: {exc}") from exc
        if stat.S_ISLNK(facts.st_mode) or not stat.S_ISREG(facts.st_mode):
            return None
        return marker

    def _scan_device(self, device_dir: Path) -> None:
        current_markers: set[Path] = set()
        current_unready: set[Path] = set()
        try:
            with os.scandir(device_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError as exc:
                        raise SourceValidationError(
                            f"cannot inspect canonical job directory: {exc}"
                        ) from exc
                    job_dir = contained_path(self.root, Path(entry.path))
                    marker = self._regular_marker(job_dir)
                    if marker is None:
                        current_unready.add(job_dir)
                    else:
                        current_markers.add(marker)
        except OSError as exc:
            raise SourceValidationError(f"cannot traverse canonical device spool: {exc}") from exc

        old_markers = self._markers_by_device.get(device_dir, set())
        self._markers_by_device[device_dir] = current_markers
        stale_unready = {job for job in self._unready_jobs if job.parent == device_dir}
        self._unready_jobs.difference_update(stale_unready)
        self._unready_jobs.update(current_unready)
        # Keep stable Path identities for the common unchanged case.
        if current_markers == old_markers:
            self._markers_by_device[device_dir] = old_markers

    def _refresh_discovery_index(self) -> None:
        try:
            root_facts = self.root.stat(follow_symlinks=False)
        except OSError as exc:
            raise SourceValidationError(f"cannot inspect canonical spool root: {exc}") from exc
        if not stat.S_ISDIR(root_facts.st_mode):
            raise SourceValidationError("canonical spool root is no longer a directory")

        if root_facts.st_mtime_ns != self._root_mtime_ns:
            discovered_devices: set[Path] = set()
            try:
                with os.scandir(self.root) as entries:
                    for entry in entries:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                        discovered_devices.add(contained_path(self.root, Path(entry.path)))
            except OSError as exc:
                raise SourceValidationError(
                    f"cannot traverse canonical spool root: {exc}"
                ) from exc
            removed = set(self._device_mtimes) - discovered_devices
            for device_dir in removed:
                self._device_mtimes.pop(device_dir, None)
                self._markers_by_device.pop(device_dir, None)
                self._unready_jobs = {
                    job for job in self._unready_jobs if job.parent != device_dir
                }
            for device_dir in discovered_devices:
                self._device_mtimes.setdefault(device_dir, -1)
            self._root_mtime_ns = root_facts.st_mtime_ns

        for device_dir, previous_mtime in tuple(self._device_mtimes.items()):
            try:
                facts = device_dir.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SourceValidationError(
                    f"cannot inspect canonical device spool: {exc}"
                ) from exc
            if not stat.S_ISDIR(facts.st_mode):
                continue
            if facts.st_mtime_ns != previous_mtime:
                self._scan_device(device_dir)
                # Capture the post-scan mtime. If publication raced with this
                # scan, the next poll observes another change and scans again.
                self._device_mtimes[device_dir] = device_dir.stat(
                    follow_symlinks=False
                ).st_mtime_ns

        # `.ready` is intentionally published after the job-directory rename;
        # that changes the child mtime, not necessarily the device directory.
        # Rechecking only the small set of not-yet-ready jobs closes that race.
        for job_dir in tuple(self._unready_jobs):
            if not job_dir.exists():
                self._unready_jobs.discard(job_dir)
                continue
            marker = self._regular_marker(job_dir)
            if marker is not None:
                self._markers_by_device.setdefault(job_dir.parent, set()).add(marker)
                self._unready_jobs.discard(job_dir)

    def discover(self, *, maximum: int) -> Sequence[ImportCandidate]:
        if maximum < 1:
            raise ValueError("maximum must be positive")
        self._refresh_discovery_index()
        markers = tuple(
            sorted(marker for values in self._markers_by_device.values() for marker in values)
        )
        selected, self._discovery_cursor = _rotating_markers(
            markers, maximum=maximum, cursor=self._discovery_cursor
        )
        return tuple(
            ImportCandidate(
                SourceKind.RETAILPRINTGUARD_CAPTURE_V1,
                self.source_instance_id,
                f"{self.source_instance_id}:canonical:{marker.parent.relative_to(self.root).as_posix()}",
                marker.parent,
            )
            for marker in selected
        )

    def load(self, candidate: ImportCandidate) -> NormalizedEnvelope:
        if candidate.source_kind is not SourceKind.RETAILPRINTGUARD_CAPTURE_V1:
            raise SourceValidationError("candidate kind does not match canonical capture adapter")
        job_dir = contained_path(self.root, candidate.source_path)
        ready_path = safe_child(self.root, job_dir, ".ready")
        ready_raw, ready = read_json_object(
            self.root,
            ready_path,
            max_bytes=_MAX_MARKER_BYTES,
            label="canonical ready marker",
        )
        if require_int(ready.get("schema_version"), "ready.schema_version", minimum=1) != 1:
            raise SourceValidationError("unsupported canonical ready schema")
        job_id = require_uuid(ready.get("job_id"), "ready.job_id")
        expected_manifest_hash = require_sha256(
            ready.get("manifest_sha256"), "ready.manifest_sha256"
        )

        manifest_path = safe_child(self.root, job_dir, "manifest.json")
        manifest_raw, manifest = read_json_object(
            self.root,
            manifest_path,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="canonical capture manifest",
        )
        if not hmac.compare_digest(_digest(manifest_raw), expected_manifest_hash):
            raise SourceValidationError("canonical ready marker does not authenticate manifest")
        if require_int(manifest.get("schema_version"), "manifest.schema_version", minimum=1) != 1:
            raise SourceValidationError("unsupported canonical capture schema")
        if manifest.get("capture_format") != CAPTURE_FORMAT:
            raise SourceValidationError(
                f"unsupported canonical capture format: {manifest.get('capture_format')!r}"
            )
        session = _mapping(manifest.get("session"), "manifest.session")
        if require_uuid(session.get("job_id"), "session.job_id") != job_id:
            raise SourceValidationError("ready and session job identifiers differ")
        session_id = require_uuid(session.get("session_id"), "session.session_id")
        device_id = require_string(session.get("device_id"), "session.device_id", maximum=64)
        relative_parts = job_dir.relative_to(self.root).parts
        if not relative_parts or relative_parts[0] != device_id:
            raise SourceValidationError("canonical job path is outside its device namespace")
        device_type = require_string(session.get("device_type"), "session.device_type", maximum=16)
        if device_type not in {"pos", "rch"}:
            raise SourceValidationError(f"unsupported canonical device type: {device_type!r}")
        parser = require_string(session.get("parser"), "session.parser", maximum=64)
        source_endpoint = endpoint(
            _mapping(session.get("client_endpoint"), "session.client_endpoint").get("host"),
            _mapping(session.get("client_endpoint"), "session.client_endpoint").get("port"),
            "session.client_endpoint",
        )
        proxy_endpoint = endpoint(
            _mapping(session.get("listener_endpoint"), "session.listener_endpoint").get("host"),
            _mapping(session.get("listener_endpoint"), "session.listener_endpoint").get("port"),
            "session.listener_endpoint",
        )
        target_value = _mapping(session.get("target_endpoint"), "session.target_endpoint")
        device_endpoint = endpoint(
            target_value.get("host"), target_value.get("port"), "session.target_endpoint"
        )
        configured_id = resolve_device(self.devices_by_target, device_endpoint)
        if configured_id != device_id:
            raise SourceValidationError("canonical device id does not match configured endpoint")
        opened_at = parse_datetime(session.get("connected_at_utc"), "session.connected_at_utc")
        require_int(session.get("connected_monotonic_ns"), "session.connected_monotonic_ns")
        closed_at = parse_datetime(manifest.get("closed_at_utc"), "manifest.closed_at_utc")
        if closed_at < opened_at:
            raise SourceValidationError("canonical close timestamp precedes connection open")
        require_int(manifest.get("closed_monotonic_ns"), "manifest.closed_monotonic_ns")

        files = _mapping(manifest.get("files"), "manifest.files")
        client_path, client_raw, client_hash = _file_snapshot(
            self.root,
            job_dir,
            files,
            "client.raw",
            limit=self.max_payload_bytes,
        )
        device_path, device_raw, device_hash = _file_snapshot(
            self.root,
            job_dir,
            files,
            "device.raw",
            limit=self.max_payload_bytes,
        )
        if len(client_raw) + len(device_raw) > self.max_payload_bytes:
            raise SourceValidationError("combined canonical RAW exceeds ingestion byte limit")
        timeline_path, timeline_raw, timeline_hash = _file_snapshot(
            self.root, job_dir, files, "timeline.jsonl", limit=_MAX_TIMELINE_BYTES
        )
        session_path, session_raw, session_hash = _file_snapshot(
            self.root, job_dir, files, "session.json", limit=_MAX_SESSION_BYTES
        )
        session_document = parse_json_bytes(session_raw, label="canonical session descriptor")
        if not isinstance(session_document, dict) or session_document != {
            "schema_version": 1,
            "session": session,
        }:
            raise SourceValidationError("canonical session descriptor differs from manifest")
        status = require_string(manifest.get("status"), "manifest.status", maximum=16)
        if status not in {"COMPLETE", "PARTIAL"}:
            raise SourceValidationError(f"unsupported canonical capture status: {status!r}")
        transport_complete = require_bool(
            manifest.get("transport_complete"), "manifest.transport_complete"
        )
        storage_complete = require_bool(
            manifest.get("storage_complete"), "manifest.storage_complete"
        )
        errors_value = manifest.get("errors")
        if not isinstance(errors_value, list) or len(errors_value) > 1_000:
            raise SourceValidationError("manifest.errors must be a bounded array")
        errors = tuple(
            require_string(item, "manifest.errors[]", maximum=4096) for item in errors_value
        )
        require_int(manifest.get("dropped_chunks"), "manifest.dropped_chunks")
        require_int(manifest.get("dropped_bytes"), "manifest.dropped_bytes")
        observed_bytes = _string_int_map(manifest.get("observed_bytes"), "manifest.observed_bytes")
        observed_chunks = _string_int_map(
            manifest.get("observed_chunks"), "manifest.observed_chunks"
        )
        captured_bytes = _string_int_map(manifest.get("captured_bytes"), "manifest.captured_bytes")
        captured_chunks = _string_int_map(
            manifest.get("captured_chunks"), "manifest.captured_chunks"
        )
        close_reason = require_string(
            manifest.get("close_reason"), "manifest.close_reason", maximum=128
        )
        allow_trailing_raw = (
            status == "PARTIAL"
            and not storage_complete
            and close_reason == "recovered_after_unclean_shutdown"
        )
        chunks = _parse_timeline(
            timeline_raw,
            client_raw=client_raw,
            device_raw=device_raw,
            session_id=session_id,
            job_id=job_id,
            device_id=device_id,
            manifest=manifest,
            allow_trailing_raw=allow_trailing_raw,
        )
        complete = status == "COMPLETE" and transport_complete and storage_complete
        if complete and (
            errors
            or observed_bytes != captured_bytes
            or observed_chunks != captured_chunks
            or manifest.get("dropped_chunks") != 0
            or manifest.get("dropped_bytes") != 0
        ):
            raise SourceValidationError("canonical COMPLETE status contradicts capture evidence")

        artifacts = (
            ArtifactSnapshot(
                ArtifactRole.REQUEST_RAW,
                client_path,
                client_hash,
                len(client_raw),
                client_raw,
                storage_complete,
            ),
            ArtifactSnapshot(
                ArtifactRole.RESPONSE_RAW,
                device_path,
                device_hash,
                len(device_raw),
                device_raw,
                storage_complete,
            ),
            ArtifactSnapshot(
                ArtifactRole.RECEIVE_TIMELINE,
                timeline_path,
                timeline_hash,
                len(timeline_raw),
                timeline_raw,
                storage_complete,
                "application/x-ndjson",
            ),
            ArtifactSnapshot(
                ArtifactRole.SESSION_DESCRIPTOR,
                session_path,
                session_hash,
                len(session_raw),
                session_raw,
                True,
                "application/json",
            ),
            ArtifactSnapshot(
                ArtifactRole.CAPTURE_MANIFEST,
                manifest_path,
                expected_manifest_hash,
                len(manifest_raw),
                manifest_raw,
                True,
                "application/json",
            ),
            ArtifactSnapshot(
                ArtifactRole.CAPTURE_READY_MARKER,
                ready_path,
                _digest(ready_raw),
                len(ready_raw),
                ready_raw,
                True,
                "application/json",
            ),
        )
        ready_after = read_regular_file(self.root, ready_path, max_bytes=_MAX_MARKER_BYTES)
        if ready_after != ready_raw:
            raise SourceBusyError("canonical ready marker changed during snapshot")
        return NormalizedEnvelope(
            source_key=(
                f"canonical:{self.source_instance_id}:{device_id}:{job_id}:{expected_manifest_hash}"
            ),
            source_kind=SourceKind.RETAILPRINTGUARD_CAPTURE_V1,
            source_instance_id=self.source_instance_id,
            device_id=device_id,
            source_job_id=job_id,
            source_session_id=session_id,
            connection_id=session_id,
            opened_at=opened_at,
            closed_at=closed_at,
            source_endpoint=source_endpoint,
            proxy_endpoint=proxy_endpoint,
            device_endpoint=device_endpoint,
            status=status,
            complete=complete,
            boundary_source="connection_lifecycle",
            boundary_confidence=0.8,
            delivery_evidence="LOCAL_SOCKET_FORWARDING_ONLY_PHYSICAL_PRINT_UNCONFIRMED",
            manifest_sha256=expected_manifest_hash,
            parser_version=None,
            artifacts=artifacts,
            chunks=chunks,
            documents=(),
            metadata={**manifest, "parser": parser},
            warnings=errors,
        )


__all__ = ["CanonicalCaptureV1Adapter"]
