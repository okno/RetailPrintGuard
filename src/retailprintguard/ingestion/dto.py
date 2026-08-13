"""Transport-neutral immutable data transfer objects for evidence ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from retailprintguard.common.domain import DocumentType


class SourceKind(StrEnum):
    RETAILPRINTGUARD_CAPTURE_V1 = "retailprintguard.capture.v1"
    COMMERCIAL_RCH_CAPTURE_V1 = "commercialrchproxy.capture.v1"
    COMMERCIAL_RCH_PARSED_V1 = "commercialrchproxy.pharsed.v1"
    PRINTPROXY_V3 = "printproxy.archive.v3"


class ArtifactRole(StrEnum):
    REQUEST_RAW = "REQUEST_RAW"
    RESPONSE_RAW = "RESPONSE_RAW"
    RESPONSE_PREVIEW = "RESPONSE_PREVIEW"
    RECEIVE_TIMELINE = "RECEIVE_TIMELINE"
    CAPTURE_MANIFEST = "CAPTURE_MANIFEST"
    CAPTURE_READY_MARKER = "CAPTURE_READY_MARKER"
    SESSION_DESCRIPTOR = "SESSION_DESCRIPTOR"
    PARSED_METADATA = "PARSED_METADATA"
    PARSED_COMMIT_MARKER = "PARSED_COMMIT_MARKER"
    NORMALIZED_TEXT = "NORMALIZED_TEXT"
    TECHNICAL_TEXT = "TECHNICAL_TEXT"
    RECEIPT_PDF = "RECEIPT_PDF"
    SOURCE_METADATA = "SOURCE_METADATA"
    INTEGRITY_LEDGER_RECORD = "INTEGRITY_LEDGER_RECORD"
    INTEGRITY_LEDGER_LATEST_RECORD = "INTEGRITY_LEDGER_LATEST_RECORD"
    INTEGRITY_LEDGER_HEAD = "INTEGRITY_LEDGER_HEAD"


class StreamDirection(StrEnum):
    CLIENT_TO_DEVICE = "CLIENT_TO_DEVICE"
    DEVICE_TO_CLIENT = "DEVICE_TO_CLIENT"


@dataclass(frozen=True, slots=True)
class Endpoint:
    ip: str
    port: int


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    role: ArtifactRole
    original_path: Path
    sha256: str
    size: int
    content: bytes = field(repr=False)
    complete: bool = True
    media_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class StreamChunk:
    sequence: int
    direction: StreamDirection
    received_at: datetime
    received_unix_ns: int | None
    forwarded_unix_ns: int | None
    local_write_drain_unix_ns: int | None
    monotonic_ns: int | None
    job_offset: int
    session_offset: int
    byte_count: int
    sha256: str
    local_write_drain_completed: bool | None
    forward_status: str | None
    error: str | None
    observed_sequence: int | None = None
    direction_sequence: int | None = None
    event_kind: str = "data"
    forwarded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    external_id: str
    document_type: DocumentType
    subtype: str | None
    complete: bool
    evidence: str
    capture_time: datetime | None
    timezone: str | None
    source_start_offset: int | None
    source_end_offset: int | None
    source_frame_ids: tuple[int, ...]
    semantic: dict[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedEnvelope:
    """One atomic unit handed to the database repository.

    ``source_key`` is an idempotency key, not a content-deduplication key.
    Repeated live prints with identical bytes remain distinct evidence.
    """

    source_key: str
    source_kind: SourceKind
    source_instance_id: str
    device_id: str
    source_job_id: str
    source_session_id: str | None
    connection_id: str | None
    opened_at: datetime
    closed_at: datetime | None
    source_endpoint: Endpoint | None
    proxy_endpoint: Endpoint | None
    device_endpoint: Endpoint
    status: str
    complete: bool
    boundary_source: str | None
    boundary_confidence: float | None
    delivery_evidence: str | None
    manifest_sha256: str
    parser_version: str | None
    artifacts: tuple[ArtifactSnapshot, ...]
    chunks: tuple[StreamChunk, ...]
    documents: tuple[NormalizedDocument, ...]
    metadata: dict[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportCandidate:
    source_kind: SourceKind
    source_instance_id: str
    candidate_key: str
    source_path: Path
    device_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    source_instance_id: str
    candidate_key: str
    source_path: Path
    source_kind: str
    reason: str


@dataclass(frozen=True, slots=True)
class RetryRecord:
    source_key: str
    attempt: int
    delay_seconds: float
    error: str
