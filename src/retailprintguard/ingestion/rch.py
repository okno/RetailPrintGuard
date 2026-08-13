"""Strict read-only adapters for commercialRCHproxy spool v1."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from retailprintguard.common.domain import DocumentType
from retailprintguard.ingestion.adapters import (
    endpoint,
    optional_endpoint,
    parse_datetime,
    parse_optional_datetime,
    require_bool,
    require_int,
    require_optional_int,
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
    NormalizedDocument,
    NormalizedEnvelope,
    SourceKind,
    StreamChunk,
    StreamDirection,
)
from retailprintguard.ingestion.errors import SourceBusyError, SourceValidationError
from retailprintguard.ingestion.safeio import (
    contained_path,
    iter_files_no_symlinks,
    parse_json_bytes,
    read_json_object,
    read_regular_file,
    safe_child,
    validate_root,
)

CAPTURE_SCHEMA = SourceKind.COMMERCIAL_RCH_CAPTURE_V1.value
PARSED_SCHEMA = SourceKind.COMMERCIAL_RCH_PARSED_V1.value
_JOB_CODE = re.compile(r"^[0-9]{4,32}$")
_MAX_MARKER_BYTES = 65_536
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_PARSED_BYTES = 32 * 1024 * 1024
_MAX_TIMELINE_LINE_BYTES = 65_536
_MAX_DOCUMENTS = 10_000
_MAX_DISCOVERY_JOBS = 100_000
_PARSER_OUTPUT = re.compile(
    r"^[0-9]{4,32}_[CG]_[0-2][0-9]\.[0-5][0-9]\.[0-5][0-9]\.[0-9]{3}"
    r"(?:_[0-9]{2,})?\.(?:txt|pdf)$"
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=True).hexdigest()


def _match_digest(data: bytes, expected: object, label: str) -> str:
    digest = require_sha256(expected, label)
    if not hmac.compare_digest(_digest(data), digest):
        raise SourceValidationError(f"SHA-256 mismatch for {label}")
    return digest


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceValidationError(f"{label} must be a JSON object")
    return value


def _boundary_confidence(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceValidationError("manifest.job_boundary_confidence must be numeric or null")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise SourceValidationError("manifest.job_boundary_confidence must be between 0 and 1")
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


def _capture_binding(
    root: Path,
    job_dir: Path,
    *,
    max_payload_bytes: int,
    load_payloads: bool,
) -> tuple[dict[str, Any], str, tuple[ArtifactSnapshot, ...], tuple[StreamChunk, ...]]:
    job_dir = contained_path(root, job_dir)
    if not _JOB_CODE.fullmatch(job_dir.name):
        raise SourceValidationError(f"unsafe commercialRCHproxy job code: {job_dir.name!r}")
    ready_path = safe_child(root, job_dir, ".ready")
    ready_raw, ready = read_json_object(
        root,
        ready_path,
        max_bytes=_MAX_MARKER_BYTES,
        label="commercialRCHproxy ready marker",
    )
    if ready.get("schema") != CAPTURE_SCHEMA:
        raise SourceValidationError(
            f"unsupported commercialRCHproxy capture schema: {ready.get('schema')!r}"
        )
    if ready.get("codice_doc") != job_dir.name:
        raise SourceValidationError("ready marker CODICE_DOC does not match its directory")
    expected_manifest_hash = require_sha256(ready.get("manifest_sha256"), "ready.manifest_sha256")
    parse_datetime(ready.get("published_at"), "ready.published_at")

    manifest_path = safe_child(root, job_dir, "manifest.json")
    manifest_raw, manifest = read_json_object(
        root,
        manifest_path,
        max_bytes=_MAX_MANIFEST_BYTES,
        label="commercialRCHproxy capture manifest",
    )
    if not hmac.compare_digest(_digest(manifest_raw), expected_manifest_hash):
        raise SourceValidationError("ready marker does not authenticate capture manifest")
    if manifest.get("schema") != CAPTURE_SCHEMA:
        raise SourceValidationError(
            f"unsupported commercialRCHproxy capture schema: {manifest.get('schema')!r}"
        )
    if manifest.get("codice_doc") != job_dir.name:
        raise SourceValidationError("capture manifest CODICE_DOC does not match its directory")

    require_uuid(manifest.get("job_id"), "manifest.job_id")
    # Offline replay deliberately uses deterministic ``offline-<digest>``
    # identifiers.  They are bounded opaque source identifiers, not UUIDs.
    require_string(manifest.get("session_id"), "manifest.session_id", maximum=128)
    require_string(manifest.get("connection_id"), "manifest.connection_id", maximum=128)
    opened_at = parse_datetime(manifest.get("opened_at"), "manifest.opened_at")
    closed_at = parse_datetime(manifest.get("closed_at"), "manifest.closed_at")
    if closed_at < opened_at:
        raise SourceValidationError("capture close timestamp precedes open timestamp")
    require_optional_int(manifest.get("opened_unix_ns"), "manifest.opened_unix_ns")
    request_size = require_int(
        manifest.get("request_size"), "manifest.request_size", maximum=max_payload_bytes
    )
    response_size = require_int(
        manifest.get("response_size"), "manifest.response_size", maximum=max_payload_bytes
    )
    if request_size + response_size > max_payload_bytes:
        raise SourceValidationError("combined directional RAW exceeds configured ingestion limit")
    require_bool(manifest.get("raw_complete"), "manifest.raw_complete")
    require_bool(manifest.get("timeline_complete"), "manifest.timeline_complete")
    timeline_count = require_int(
        manifest.get("timeline_event_count"),
        "manifest.timeline_event_count",
        maximum=100_000,
    )
    status = require_string(manifest.get("status"), "manifest.status", maximum=128)
    if status not in {"ready", "ready_capture_incomplete"}:
        raise SourceValidationError(f"unsupported capture publication status: {status!r}")

    files = _require_mapping(manifest.get("files"), "manifest.files")
    hashes = _require_mapping(manifest.get("sha256"), "manifest.sha256")
    names = {
        "request_raw": files.get("request_raw"),
        "response_raw": files.get("response_raw"),
        "timeline": files.get("timeline"),
    }
    contents: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for key, name in names.items():
        path = safe_child(root, job_dir, name)
        limit = (
            max_payload_bytes if key != "timeline" else min(max_payload_bytes, 128 * 1024 * 1024)
        )
        content = read_regular_file(root, path, max_bytes=limit)
        _match_digest(content, hashes.get(key), f"manifest.sha256.{key}")
        contents[key] = content
        paths[key] = path
    if len(contents["request_raw"]) != request_size:
        raise SourceValidationError("request RAW size differs from capture manifest")
    if len(contents["response_raw"]) != response_size:
        raise SourceValidationError("response RAW size differs from capture manifest")

    artifacts: tuple[ArtifactSnapshot, ...] = ()
    chunks: tuple[StreamChunk, ...] = ()
    if load_payloads:
        artifacts = (
            ArtifactSnapshot(
                ArtifactRole.REQUEST_RAW,
                paths["request_raw"],
                hashes["request_raw"],
                len(contents["request_raw"]),
                contents["request_raw"],
                bool(manifest["raw_complete"]),
            ),
            ArtifactSnapshot(
                ArtifactRole.RESPONSE_RAW,
                paths["response_raw"],
                hashes["response_raw"],
                len(contents["response_raw"]),
                contents["response_raw"],
                bool(manifest["raw_complete"]),
            ),
            ArtifactSnapshot(
                ArtifactRole.RECEIVE_TIMELINE,
                paths["timeline"],
                hashes["timeline"],
                len(contents["timeline"]),
                contents["timeline"],
                bool(manifest["timeline_complete"]),
                "application/x-ndjson",
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
        chunks = _parse_timeline(
            contents["timeline"],
            request=contents["request_raw"],
            response=contents["response_raw"],
            expected_count=timeline_count,
            timeline_complete=bool(manifest["timeline_complete"]),
            session_id=str(manifest["session_id"]),
            connection_id=str(manifest["connection_id"]),
        )
    ready_after = read_regular_file(root, ready_path, max_bytes=_MAX_MARKER_BYTES)
    if ready_after != ready_raw:
        raise SourceBusyError("commercialRCHproxy ready marker changed during snapshot")
    return manifest, expected_manifest_hash, artifacts, chunks


def _parse_timeline(
    content: bytes,
    *,
    request: bytes,
    response: bytes,
    expected_count: int,
    timeline_complete: bool,
    session_id: str,
    connection_id: str,
) -> tuple[StreamChunk, ...]:
    if content and not content.endswith(b"\n"):
        raise SourceValidationError("commercialRCHproxy timeline has a truncated final record")
    events: list[StreamChunk] = []
    previous_sequence = 0
    direction_offsets = {StreamDirection.CLIENT_TO_DEVICE: 0, StreamDirection.DEVICE_TO_CLIENT: 0}
    session_ends: dict[StreamDirection, int | None] = {
        StreamDirection.CLIENT_TO_DEVICE: None,
        StreamDirection.DEVICE_TO_CLIENT: None,
    }
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        if not raw_line or len(raw_line) > _MAX_TIMELINE_LINE_BYTES:
            raise SourceValidationError(f"invalid timeline record size at line {line_number}")
        value = parse_json_bytes(raw_line, label=f"timeline line {line_number}")
        if not isinstance(value, dict):
            raise SourceValidationError(f"timeline line {line_number} must be a JSON object")
        sequence = require_int(
            value.get("sequence"), f"timeline[{line_number}].sequence", minimum=1
        )
        if sequence <= previous_sequence:
            raise SourceValidationError("timeline sequence is not strictly increasing")
        if timeline_complete and sequence != previous_sequence + 1:
            raise SourceValidationError("complete timeline sequence is not contiguous")
        previous_sequence = sequence
        raw_direction = value.get("direction")
        if raw_direction == "CLIENT -> RCH":
            direction = StreamDirection.CLIENT_TO_DEVICE
            stream = request
        elif raw_direction == "RCH -> CLIENT":
            direction = StreamDirection.DEVICE_TO_CLIENT
            stream = response
        else:
            raise SourceValidationError(f"unsupported timeline direction: {raw_direction!r}")
        offset = require_int(value.get("job_offset"), f"timeline[{line_number}].job_offset")
        session_offset = require_int(
            value.get("session_offset"), f"timeline[{line_number}].session_offset"
        )
        length = require_int(
            value.get("byte_count"), f"timeline[{line_number}].byte_count", minimum=1
        )
        if offset + length > len(stream):
            raise SourceValidationError(f"timeline line {line_number} addresses bytes outside RAW")
        if timeline_complete and offset != direction_offsets[direction]:
            raise SourceValidationError("complete timeline has a directional offset gap or overlap")
        if (
            timeline_complete
            and session_ends[direction] is not None
            and session_offset != session_ends[direction]
        ):
            raise SourceValidationError("complete timeline has a session offset gap or overlap")
        direction_offsets[direction] = offset + length
        session_ends[direction] = session_offset + length
        digest = _match_digest(
            stream[offset : offset + length],
            value.get("sha256"),
            f"timeline[{line_number}].sha256",
        )
        if value.get("session_id") != session_id or value.get("connection_id") != connection_id:
            raise SourceValidationError("timeline session/connection binding mismatch")
        local_drain = value.get("local_write_drain_completed")
        if local_drain is not None and not isinstance(local_drain, bool):
            raise SourceValidationError(
                "timeline local_write_drain_completed must be boolean or null"
            )
        events.append(
            StreamChunk(
                sequence=sequence,
                direction=direction,
                received_at=parse_datetime(
                    value.get("received_at"), f"timeline[{line_number}].received_at"
                ),
                received_unix_ns=require_optional_int(
                    value.get("received_unix_ns"), f"timeline[{line_number}].received_unix_ns"
                ),
                forwarded_unix_ns=require_optional_int(
                    value.get("forwarded_unix_ns"), f"timeline[{line_number}].forwarded_unix_ns"
                ),
                local_write_drain_unix_ns=require_optional_int(
                    value.get("local_write_drain_unix_ns"),
                    f"timeline[{line_number}].local_write_drain_unix_ns",
                ),
                monotonic_ns=require_optional_int(
                    value.get("monotonic_ns"), f"timeline[{line_number}].monotonic_ns"
                ),
                job_offset=offset,
                session_offset=session_offset,
                byte_count=length,
                sha256=digest,
                local_write_drain_completed=local_drain,
                forward_status=require_optional_string(
                    value.get("forward_status"),
                    f"timeline[{line_number}].forward_status",
                    maximum=128,
                ),
                error=require_optional_string(
                    value.get("error"), f"timeline[{line_number}].error", maximum=4096
                ),
            )
        )
    if len(events) != expected_count:
        raise SourceValidationError(
            f"timeline event count mismatch: manifest={expected_count}, observed={len(events)}"
        )
    if timeline_complete and (
        direction_offsets[StreamDirection.CLIENT_TO_DEVICE] != len(request)
        or direction_offsets[StreamDirection.DEVICE_TO_CLIENT] != len(response)
    ):
        raise SourceValidationError("complete timeline does not cover both directional RAW files")
    return tuple(events)


class RCHCaptureV1Adapter:
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

    def discover(self, *, maximum: int) -> Sequence[ImportCandidate]:
        markers = iter_files_no_symlinks(self.root, ".ready", maximum=_MAX_DISCOVERY_JOBS)
        selected, self._discovery_cursor = _rotating_markers(
            markers, maximum=maximum, cursor=self._discovery_cursor
        )
        return tuple(
            ImportCandidate(
                SourceKind.COMMERCIAL_RCH_CAPTURE_V1,
                self.source_instance_id,
                f"{self.source_instance_id}:rch-capture:{marker.parent.relative_to(self.root).as_posix()}",
                marker.parent,
            )
            for marker in selected
        )

    def load(self, candidate: ImportCandidate) -> NormalizedEnvelope:
        if candidate.source_kind is not SourceKind.COMMERCIAL_RCH_CAPTURE_V1:
            raise SourceValidationError("candidate kind does not match RCH capture adapter")
        manifest, manifest_hash, artifacts, chunks = _capture_binding(
            self.root,
            candidate.source_path,
            max_payload_bytes=self.max_payload_bytes,
            load_payloads=True,
        )
        device_endpoint = endpoint(
            manifest.get("printer_ip"), manifest.get("printer_port"), "printer"
        )
        source_endpoint = optional_endpoint(
            manifest.get("client_ip"), manifest.get("client_port"), "client"
        )
        proxy_endpoint = endpoint(manifest.get("listen_ip"), manifest.get("listen_port"), "proxy")
        device_id = resolve_device(self.devices_by_target, device_endpoint)
        job_id = str(manifest["job_id"])
        return NormalizedEnvelope(
            source_key=f"rch-capture:{self.source_instance_id}:{job_id}:{manifest_hash}",
            source_kind=SourceKind.COMMERCIAL_RCH_CAPTURE_V1,
            source_instance_id=self.source_instance_id,
            device_id=device_id,
            source_job_id=job_id,
            source_session_id=str(manifest["session_id"]),
            connection_id=str(manifest["connection_id"]),
            opened_at=parse_datetime(manifest["opened_at"], "manifest.opened_at"),
            closed_at=parse_optional_datetime(manifest.get("closed_at"), "manifest.closed_at"),
            source_endpoint=source_endpoint,
            proxy_endpoint=proxy_endpoint,
            device_endpoint=device_endpoint,
            status=str(manifest["status"]),
            complete=bool(manifest["raw_complete"] and manifest["timeline_complete"]),
            boundary_source=require_optional_string(
                manifest.get("job_boundary_source"), "manifest.job_boundary_source", maximum=128
            ),
            boundary_confidence=_boundary_confidence(manifest.get("job_boundary_confidence")),
            delivery_evidence=require_optional_string(
                manifest.get("delivery_evidence"), "manifest.delivery_evidence", maximum=128
            ),
            manifest_sha256=manifest_hash,
            parser_version=None,
            artifacts=artifacts,
            chunks=chunks,
            documents=(),
            metadata=manifest,
            warnings=tuple(
                str(value)
                for value in (manifest.get("capture_error"), manifest.get("timeline_error"))
                if isinstance(value, str) and value
            ),
        )


class RCHParsedV1Adapter:
    def __init__(
        self,
        root: Path,
        *,
        source_instance_id: str,
        devices_by_target: Mapping[tuple[str, int], str],
        max_payload_bytes: int = 128 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_payload_bytes < 1 or max_output_bytes < 1:
            raise ValueError("ingestion byte limits must be positive")
        self.root = validate_root(root)
        self.source_instance_id = validate_source_instance_id(source_instance_id)
        self.devices_by_target = dict(devices_by_target)
        self.max_payload_bytes = max_payload_bytes
        self.max_output_bytes = max_output_bytes
        self._discovery_cursor = 0

    def discover(self, *, maximum: int) -> Sequence[ImportCandidate]:
        markers = iter_files_no_symlinks(self.root, ".parsed", maximum=_MAX_DISCOVERY_JOBS)
        selected, self._discovery_cursor = _rotating_markers(
            markers, maximum=maximum, cursor=self._discovery_cursor
        )
        return tuple(
            ImportCandidate(
                SourceKind.COMMERCIAL_RCH_PARSED_V1,
                self.source_instance_id,
                f"{self.source_instance_id}:rch-parsed:{marker.parent.relative_to(self.root).as_posix()}",
                marker.parent,
            )
            for marker in selected
        )

    def load(self, candidate: ImportCandidate) -> NormalizedEnvelope:
        if candidate.source_kind is not SourceKind.COMMERCIAL_RCH_PARSED_V1:
            raise SourceValidationError("candidate kind does not match RCH parsed adapter")
        job_dir = contained_path(self.root, candidate.source_path)
        manifest, manifest_hash, _artifacts, _chunks = _capture_binding(
            self.root,
            job_dir,
            max_payload_bytes=self.max_payload_bytes,
            load_payloads=False,
        )
        marker_path = safe_child(self.root, job_dir, ".parsed")
        marker_raw, marker = read_json_object(
            self.root,
            marker_path,
            max_bytes=_MAX_MARKER_BYTES,
            label="commercialRCHproxy parsed marker",
        )
        if marker.get("schema") != PARSED_SCHEMA:
            raise SourceValidationError(
                f"unsupported commercialRCHproxy parsed schema: {marker.get('schema')!r}"
            )
        if marker.get("status") != "parsed":
            raise SourceValidationError("commercialRCHproxy parsed marker is not committed")
        if marker.get("codice_doc") != job_dir.name:
            raise SourceValidationError("parsed marker CODICE_DOC does not match job directory")
        if marker.get("metadata") != "PHARSED/parsed.json":
            raise SourceValidationError(
                "parsed marker metadata path is not the v1 PHARSED contract"
            )
        metadata_hash = require_sha256(marker.get("metadata_sha256"), "parsed.metadata_sha256")
        parser_version = require_string(
            marker.get("parser_version"), "parsed.parser_version", maximum=64
        )
        document_count = require_int(
            marker.get("document_count"), "parsed.document_count", maximum=_MAX_DOCUMENTS
        )
        parse_datetime(marker.get("completed_at"), "parsed.completed_at")

        output_dir = contained_path(self.root, job_dir / "PHARSED")
        parsed_path = safe_child(self.root, output_dir, "parsed.json")
        parsed_raw, parsed = read_json_object(
            self.root,
            parsed_path,
            max_bytes=_MAX_PARSED_BYTES,
            label="commercialRCHproxy parsed metadata",
        )
        if not hmac.compare_digest(_digest(parsed_raw), metadata_hash):
            raise SourceValidationError("parsed marker does not authenticate PHARSED/parsed.json")
        if parsed.get("schema") != PARSED_SCHEMA:
            raise SourceValidationError(
                f"unsupported commercialRCHproxy parsed schema: {parsed.get('schema')!r}"
            )
        if parsed.get("codice_doc") != job_dir.name:
            raise SourceValidationError("parsed metadata CODICE_DOC does not match job directory")
        if parsed.get("parser_version") != parser_version:
            raise SourceValidationError("parsed marker and metadata parser versions differ")
        if parsed.get("capture_manifest") != "../manifest.json":
            raise SourceValidationError("parsed metadata capture path is not the v1 binding")
        if parsed.get("capture_manifest_sha256") != manifest_hash:
            raise SourceValidationError("parsed metadata is bound to a different capture manifest")
        raw_documents = parsed.get("documents")
        if not isinstance(raw_documents, list) or len(raw_documents) != document_count:
            raise SourceValidationError("parsed document count is inconsistent")
        protocol_issues = parsed.get("protocol_issues")
        correlations = parsed.get("correlations")
        evidence_policy = parsed.get("evidence_policy")
        if not isinstance(protocol_issues, list) or not isinstance(correlations, list):
            raise SourceValidationError("parsed issues and correlations must be arrays")
        if any(not isinstance(item, dict) for item in (*protocol_issues, *correlations)):
            raise SourceValidationError("parsed issues and correlations must contain objects")
        if not isinstance(evidence_policy, dict):
            raise SourceValidationError("parsed evidence_policy must be an object")

        artifacts: list[ArtifactSnapshot] = [
            ArtifactSnapshot(
                ArtifactRole.PARSED_METADATA,
                parsed_path,
                metadata_hash,
                len(parsed_raw),
                parsed_raw,
                True,
                "application/json",
            )
        ]
        documents: list[NormalizedDocument] = []
        seen_document_ids: set[str] = set()
        seen_outputs: set[str] = set()
        for index, raw_document in enumerate(raw_documents, 1):
            document = _require_mapping(raw_document, f"documents[{index}]")
            external_id = require_string(
                document.get("document_id"), f"documents[{index}].document_id", maximum=128
            )
            if external_id in seen_document_ids:
                raise SourceValidationError(f"duplicate parsed document ID: {external_id}")
            seen_document_ids.add(external_id)
            if (
                require_int(document.get("ordinal"), f"documents[{index}].ordinal", minimum=1)
                != index
            ):
                raise SourceValidationError("parsed document ordinals are not contiguous")
            kind = require_string(document.get("type"), f"documents[{index}].type", maximum=8)
            canonical = require_string(
                document.get("canonical_type"), f"documents[{index}].canonical_type", maximum=64
            )
            if (kind, canonical) not in {("C", "commerciale"), ("G", "gestionale")}:
                raise SourceValidationError(
                    f"documents[{index}] has inconsistent C/G classification"
                )
            source = _require_mapping(document.get("source"), f"documents[{index}].source")
            frame_ids_raw = source.get("frame_ids")
            if not isinstance(frame_ids_raw, list) or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 1
                for item in frame_ids_raw
            ):
                raise SourceValidationError(f"documents[{index}].source.frame_ids is invalid")
            if not frame_ids_raw:
                raise SourceValidationError(f"documents[{index}].source.frame_ids is empty")
            semantic = _require_mapping(document.get("semantic"), f"documents[{index}].semantic")
            source_start = require_optional_int(
                source.get("start_offset"), f"documents[{index}].source.start_offset"
            )
            source_end = require_optional_int(
                source.get("end_offset"), f"documents[{index}].source.end_offset"
            )
            if (
                source_start is None
                or source_end is None
                or source_end <= source_start
                or source_end > int(manifest["request_size"])
            ):
                raise SourceValidationError(
                    f"documents[{index}] source range is outside request RAW"
                )
            issues = semantic.get("issues")
            if issues is not None and (
                not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues)
            ):
                raise SourceValidationError(f"documents[{index}].semantic.issues is invalid")
            warnings = (
                tuple(
                    str(issue.get("code"))
                    for issue in issues
                    if isinstance(issues, list)
                    and isinstance(issue, dict)
                    and isinstance(issue.get("code"), str)
                )
                if isinstance(issues, list)
                else ()
            )
            documents.append(
                NormalizedDocument(
                    external_id=external_id,
                    document_type=(
                        DocumentType.COMMERCIAL_DOCUMENT
                        if kind == "C"
                        else DocumentType.MANAGEMENT_DOCUMENT
                    ),
                    subtype=require_optional_string(
                        document.get("subtype"), f"documents[{index}].subtype", maximum=128
                    ),
                    complete=require_bool(document.get("complete"), f"documents[{index}].complete"),
                    evidence=require_string(
                        document.get("classification_evidence"),
                        f"documents[{index}].classification_evidence",
                        maximum=128,
                    ),
                    capture_time=parse_optional_datetime(
                        document.get("capture_time_local"),
                        f"documents[{index}].capture_time_local",
                    ),
                    timezone=require_optional_string(
                        document.get("timezone"), f"documents[{index}].timezone", maximum=64
                    ),
                    source_start_offset=source_start,
                    source_end_offset=source_end,
                    source_frame_ids=tuple(frame_ids_raw),
                    semantic=semantic,
                    warnings=warnings,
                )
            )
            outputs = _require_mapping(document.get("outputs"), f"documents[{index}].outputs")
            for output_key, role, media_type in (
                ("txt", ArtifactRole.NORMALIZED_TEXT, "text/plain; charset=utf-8"),
                ("pdf", ArtifactRole.RECEIPT_PDF, "application/pdf"),
            ):
                output = outputs.get(output_key)
                if output is None:
                    continue
                output_map = _require_mapping(output, f"documents[{index}].outputs.{output_key}")
                name = require_string(
                    output_map.get("name"),
                    f"documents[{index}].outputs.{output_key}.name",
                    maximum=255,
                )
                if not _PARSER_OUTPUT.fullmatch(name):
                    raise SourceValidationError(f"unsafe parsed output filename: {name}")
                if not name.startswith(f"{job_dir.name}_{kind}_"):
                    raise SourceValidationError("parsed output filename/type binding mismatch")
                if not name.endswith(f".{output_key}"):
                    raise SourceValidationError("parsed output filename/suffix binding mismatch")
                if name in seen_outputs:
                    raise SourceValidationError(f"duplicate parsed output filename: {name}")
                seen_outputs.add(name)
                path = safe_child(self.root, output_dir, name)
                content = read_regular_file(self.root, path, max_bytes=self.max_output_bytes)
                digest = _match_digest(
                    content,
                    output_map.get("sha256"),
                    f"documents[{index}].outputs.{output_key}.sha256",
                )
                artifacts.append(
                    ArtifactSnapshot(role, path, digest, len(content), content, True, media_type)
                )

        artifacts.append(
            ArtifactSnapshot(
                ArtifactRole.PARSED_COMMIT_MARKER,
                marker_path,
                _digest(marker_raw),
                len(marker_raw),
                marker_raw,
                True,
                "application/json",
            )
        )
        device_endpoint = endpoint(
            manifest.get("printer_ip"), manifest.get("printer_port"), "printer"
        )
        device_id = resolve_device(self.devices_by_target, device_endpoint)
        marker_after = read_regular_file(self.root, marker_path, max_bytes=_MAX_MARKER_BYTES)
        if marker_after != marker_raw:
            raise SourceBusyError("commercialRCHproxy parsed marker changed during snapshot")
        return NormalizedEnvelope(
            source_key=(
                f"rch-parsed:{self.source_instance_id}:{manifest['job_id']}:"
                f"{parser_version}:{metadata_hash}"
            ),
            source_kind=SourceKind.COMMERCIAL_RCH_PARSED_V1,
            source_instance_id=self.source_instance_id,
            device_id=device_id,
            source_job_id=str(manifest["job_id"]),
            source_session_id=str(manifest["session_id"]),
            connection_id=str(manifest["connection_id"]),
            opened_at=parse_datetime(manifest["opened_at"], "manifest.opened_at"),
            closed_at=parse_optional_datetime(manifest.get("closed_at"), "manifest.closed_at"),
            source_endpoint=optional_endpoint(
                manifest.get("client_ip"), manifest.get("client_port"), "client"
            ),
            proxy_endpoint=endpoint(
                manifest.get("listen_ip"), manifest.get("listen_port"), "proxy"
            ),
            device_endpoint=device_endpoint,
            status=require_string(parsed.get("parser_status"), "parsed.parser_status", maximum=256),
            complete=all(document.complete for document in documents),
            boundary_source=require_optional_string(
                manifest.get("job_boundary_source"), "manifest.job_boundary_source", maximum=128
            ),
            boundary_confidence=_boundary_confidence(manifest.get("job_boundary_confidence")),
            delivery_evidence=require_optional_string(
                manifest.get("delivery_evidence"), "manifest.delivery_evidence", maximum=128
            ),
            manifest_sha256=manifest_hash,
            parser_version=parser_version,
            artifacts=tuple(artifacts),
            chunks=(),
            documents=tuple(documents),
            metadata=parsed,
            warnings=tuple(
                str(issue.get("code"))
                for issue in protocol_issues
                if isinstance(issue, dict) and isinstance(issue.get("code"), str)
            ),
        )
