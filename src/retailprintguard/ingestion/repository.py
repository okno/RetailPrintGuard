"""Database boundary for atomic, idempotent ingestion."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from retailprintguard.ingestion.dto import (
    NormalizedEnvelope,
    QuarantineRecord,
    RetryRecord,
)


class ImportDisposition(StrEnum):
    IMPORTED = "IMPORTED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class RepositoryImportResult:
    disposition: ImportDisposition
    record_id: str | None = None


@runtime_checkable
class IngestionRepository(Protocol):
    """Persistence API implemented by the MariaDB layer.

    ``store_import`` must perform all inserts and the unique ``source_key``
    claim in one transaction. A uniqueness race returns ``DUPLICATE``; it
    must never create a second logical import.
    """

    def store_import(self, envelope: NormalizedEnvelope) -> RepositoryImportResult: ...

    def record_retry(self, retry: RetryRecord) -> None: ...

    def quarantine(self, record: QuarantineRecord) -> None: ...


class MemoryIngestionRepository:
    """Thread-safe reference implementation used for validation and tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.imports: dict[str, NormalizedEnvelope] = {}
        self.retries: list[RetryRecord] = []
        self.quarantines: dict[str, QuarantineRecord] = {}

    def store_import(self, envelope: NormalizedEnvelope) -> RepositoryImportResult:
        with self._lock:
            if envelope.source_key in self.imports:
                return RepositoryImportResult(ImportDisposition.DUPLICATE, envelope.source_key)
            self.imports[envelope.source_key] = envelope
            return RepositoryImportResult(ImportDisposition.IMPORTED, envelope.source_key)

    def record_retry(self, retry: RetryRecord) -> None:
        with self._lock:
            self.retries.append(retry)

    def quarantine(self, record: QuarantineRecord) -> None:
        with self._lock:
            self.quarantines.setdefault(record.candidate_key, record)


class ValidationIngestionRepository:
    """Bounded-memory sink for ``--validate-only`` scans.

    The sink deliberately retains neither keys nor payload bytes.  It is not a
    durable/idempotent repository and is therefore exposed only through the
    explicit validation mode.
    """

    def store_import(self, envelope: NormalizedEnvelope) -> RepositoryImportResult:
        del envelope
        return RepositoryImportResult(ImportDisposition.IMPORTED)

    def record_retry(self, retry: RetryRecord) -> None:
        del retry

    def quarantine(self, record: QuarantineRecord) -> None:
        del record
