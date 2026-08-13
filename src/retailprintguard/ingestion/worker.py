"""Failure-isolated ingestion worker with bounded exponential backoff."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from retailprintguard.ingestion.adapters import SourceAdapter
from retailprintguard.ingestion.dto import NormalizedEnvelope, QuarantineRecord, RetryRecord
from retailprintguard.ingestion.errors import (
    RepositoryContractError,
    RepositoryUnavailable,
    SourceBusyError,
    SourceValidationError,
)
from retailprintguard.ingestion.repository import ImportDisposition, IngestionRepository


@dataclass(slots=True)
class IngestionRunReport:
    discovered: int = 0
    imported: int = 0
    duplicates: int = 0
    quarantined: int = 0
    retry_exhausted: int = 0
    source_busy: int = 0
    errors: list[str] = field(default_factory=list)
    # Internal progress token used by the one-shot historical drainer. It is
    # intentionally omitted from CLI/API serialization because source keys can
    # contain deployment paths.
    candidate_keys: set[str] = field(default_factory=set, repr=False)

    def merge(self, other: IngestionRunReport) -> None:
        self.discovered += other.discovered
        self.imported += other.imported
        self.duplicates += other.duplicates
        self.quarantined += other.quarantined
        self.retry_exhausted += other.retry_exhausted
        self.source_busy += other.source_busy
        self.errors.extend(other.errors)
        self.candidate_keys.update(other.candidate_keys)


class IngestionWorker:
    def __init__(
        self,
        repository: IngestionRepository,
        adapters: Sequence[SourceAdapter],
        *,
        retry_initial_seconds: float = 2.0,
        retry_max_seconds: float = 300.0,
        retry_attempts: int = 5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if retry_initial_seconds <= 0 or retry_max_seconds < retry_initial_seconds:
            raise ValueError("invalid retry interval")
        if retry_attempts < 1 or retry_attempts > 100:
            raise ValueError("retry_attempts must be between 1 and 100")
        self.repository = repository
        self.adapters = tuple(adapters)
        self.retry_initial_seconds = retry_initial_seconds
        self.retry_max_seconds = retry_max_seconds
        self.retry_attempts = retry_attempts
        self.sleeper = sleeper

    def _quarantine(
        self,
        *,
        source_instance_id: str,
        candidate_key: str,
        source_path: Path,
        source_kind: str,
        reason: str,
        report: IngestionRunReport,
    ) -> None:
        record = QuarantineRecord(
            source_instance_id,
            candidate_key,
            source_path,
            source_kind,
            reason[:4096],
        )
        delay = self.retry_initial_seconds
        for attempt in range(1, self.retry_attempts + 1):
            try:
                self.repository.quarantine(record)
            except RepositoryUnavailable as exc:
                retry = RetryRecord(
                    candidate_key, attempt, delay, f"{type(exc).__name__}: {exc}"[:4096]
                )
                with suppress(RepositoryUnavailable):
                    self.repository.record_retry(retry)
                if attempt == self.retry_attempts:
                    report.retry_exhausted += 1
                    report.errors.append(
                        f"repository unavailable while quarantining {candidate_key}: {exc}"
                    )
                    return
                self.sleeper(delay)
                delay = min(self.retry_max_seconds, delay * 2)
                continue
            report.quarantined += 1
            return

    def _store_with_retry(self, envelope: NormalizedEnvelope, report: IngestionRunReport) -> None:
        source_key = envelope.source_key
        delay = self.retry_initial_seconds
        for attempt in range(1, self.retry_attempts + 1):
            try:
                result = self.repository.store_import(envelope)
            except RepositoryUnavailable as exc:
                retry = RetryRecord(
                    source_key, attempt, delay, f"{type(exc).__name__}: {exc}"[:4096]
                )
                with suppress(RepositoryUnavailable):
                    self.repository.record_retry(retry)
                if attempt == self.retry_attempts:
                    report.retry_exhausted += 1
                    report.errors.append(f"repository retry exhausted for {source_key}: {exc}")
                    return
                self.sleeper(delay)
                delay = min(self.retry_max_seconds, delay * 2)
                continue
            if result.disposition is ImportDisposition.IMPORTED:
                report.imported += 1
            elif result.disposition is ImportDisposition.DUPLICATE:
                report.duplicates += 1
            else:
                raise RepositoryContractError(
                    f"repository returned unsupported disposition: {result.disposition!r}"
                )
            return

    def _scan_adapter(self, adapter: SourceAdapter, *, maximum: int) -> IngestionRunReport:
        report = IngestionRunReport()
        try:
            candidates = adapter.discover(maximum=maximum)
        except SourceBusyError as exc:
            report.source_busy += 1
            report.errors.append(f"source snapshot busy at {adapter.root}: {exc}")
            return report
        except SourceValidationError as exc:
            self._quarantine(
                source_instance_id=adapter.source_instance_id,
                candidate_key=f"{adapter.source_instance_id}:discovery:{adapter.root}",
                source_path=adapter.root,
                source_kind=type(adapter).__name__,
                reason=f"{type(exc).__name__}: {exc}",
                report=report,
            )
            return report
        for candidate in candidates:
            report.discovered += 1
            report.candidate_keys.add(candidate.candidate_key)
            try:
                envelope = adapter.load(candidate)
            except SourceBusyError as exc:
                report.source_busy += 1
                report.errors.append(f"source candidate busy {candidate.candidate_key}: {exc}")
                continue
            except SourceValidationError as exc:
                self._quarantine(
                    source_instance_id=candidate.source_instance_id,
                    candidate_key=candidate.candidate_key,
                    source_path=candidate.source_path,
                    source_kind=candidate.source_kind.value,
                    reason=f"{type(exc).__name__}: {exc}",
                    report=report,
                )
                continue
            self._store_with_retry(envelope, report)
        return report

    def run_once(self, *, maximum_per_adapter: int = 100) -> IngestionRunReport:
        if maximum_per_adapter < 1:
            raise ValueError("maximum_per_adapter must be positive")
        report = IngestionRunReport()
        for adapter in self.adapters:
            batch_id: str | None = None
            begin_batch = getattr(self.repository, "begin_import_batch", None)
            if callable(begin_batch):
                try:
                    batch_id = begin_batch(
                        source_system=type(adapter).__name__,
                        source_instance=adapter.source_instance_id,
                        source_root=adapter.root,
                    )
                except RepositoryUnavailable as exc:
                    report.errors.append(f"cannot begin import report for {adapter.root}: {exc}")
            adapter_report = self._scan_adapter(adapter, maximum=maximum_per_adapter)
            if batch_id is not None:
                complete_batch = getattr(self.repository, "complete_import_batch", None)
                if callable(complete_batch):
                    summary = {
                        "discovered": adapter_report.discovered,
                        "imported": adapter_report.imported,
                        "duplicates": adapter_report.duplicates,
                        "quarantined": adapter_report.quarantined,
                        "retry_exhausted": adapter_report.retry_exhausted,
                        "source_busy": adapter_report.source_busy,
                        "errors": list(adapter_report.errors),
                    }
                    try:
                        complete_batch(batch_id, summary)
                    except (RepositoryUnavailable, ValueError) as exc:
                        adapter_report.errors.append(
                            f"cannot complete import report for {adapter.root}: {exc}"
                        )
            report.merge(adapter_report)
        refresh_statuses = getattr(self.repository, "refresh_device_statuses", None)
        if callable(refresh_statuses):
            try:
                refresh_statuses()
            except RepositoryUnavailable as exc:
                # Device/spool observability is deliberately secondary to durable
                # evidence import and is retried on the next scan.
                report.errors.append(f"device status refresh unavailable: {exc}")
        return report

    def run_forever(
        self,
        stop: Event,
        *,
        scan_interval_seconds: float,
        maximum_per_adapter: int,
        on_report: Callable[[IngestionRunReport], None] | None = None,
    ) -> None:
        if scan_interval_seconds <= 0:
            raise ValueError("scan_interval_seconds must be positive")
        while not stop.is_set():
            report = self.run_once(maximum_per_adapter=maximum_per_adapter)
            if on_report is not None:
                on_report(report)
            stop.wait(scan_interval_seconds)
