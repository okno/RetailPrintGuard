from __future__ import annotations

import json
from pathlib import Path

from ingestion_fixtures import (
    POS_TARGET,
    RCH_TARGET,
    TEST_HMAC_KEY,
    tree_snapshot,
    write_printproxy_job,
    write_rch_job,
)

from retailprintguard.ingestion.dto import NormalizedEnvelope, QuarantineRecord, RetryRecord
from retailprintguard.ingestion.errors import RepositoryUnavailable
from retailprintguard.ingestion.printproxy import PrintProxyV3Adapter
from retailprintguard.ingestion.rch import RCHCaptureV1Adapter
from retailprintguard.ingestion.repository import (
    MemoryIngestionRepository,
    RepositoryImportResult,
)
from retailprintguard.ingestion.worker import IngestionWorker


class TemporarilyOfflineRepository:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.inner = MemoryIngestionRepository()

    def store_import(self, envelope: NormalizedEnvelope) -> RepositoryImportResult:
        if self.failures:
            self.failures -= 1
            raise RepositoryUnavailable("synthetic database outage")
        return self.inner.store_import(envelope)

    def record_retry(self, retry: RetryRecord) -> None:
        self.inner.record_retry(retry)

    def quarantine(self, record: QuarantineRecord) -> None:
        self.inner.quarantine(record)


class BatchTrackingRepository(MemoryIngestionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.started: list[dict[str, object]] = []
        self.completed: list[tuple[str, dict[str, object]]] = []

    def begin_import_batch(self, **values: object) -> str:
        self.started.append(values)
        return "batch-1"

    def complete_import_batch(self, batch_id: str, report: dict[str, object]) -> None:
        self.completed.append((batch_id, report))


def test_database_outage_retries_without_mutating_proxy_spool_and_import_is_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rch"
    write_rch_job(source)
    source_before = tree_snapshot(source)
    repository = TemporarilyOfflineRepository(failures=2)
    delays: list[float] = []
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )
    worker = IngestionWorker(
        repository,
        [adapter],
        retry_initial_seconds=0.01,
        retry_max_seconds=0.04,
        retry_attempts=4,
        sleeper=delays.append,
    )

    first = worker.run_once(maximum_per_adapter=10)
    second = worker.run_once(maximum_per_adapter=10)

    assert first.imported == 1
    assert first.retry_exhausted == 0
    assert [record.attempt for record in repository.inner.retries] == [1, 2]
    assert delays == [0.01, 0.02]
    assert second.duplicates == 1
    assert len(repository.inner.imports) == 1
    assert tree_snapshot(source) == source_before


def test_retry_exhaustion_keeps_source_ready_for_a_later_scan(tmp_path: Path) -> None:
    source = tmp_path / "rch"
    write_rch_job(source)
    repository = TemporarilyOfflineRepository(failures=2)
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )
    worker = IngestionWorker(
        repository,
        [adapter],
        retry_initial_seconds=0.01,
        retry_max_seconds=0.01,
        retry_attempts=2,
        sleeper=lambda _delay: None,
    )

    failed = worker.run_once(maximum_per_adapter=10)
    recovered = worker.run_once(maximum_per_adapter=10)

    assert failed.retry_exhausted == 1
    assert recovered.imported == 1


def test_unknown_printproxy_schema_is_quarantined_and_another_route_still_imports(
    tmp_path: Path,
) -> None:
    source = tmp_path / "printproxy"
    invalid_route = write_printproxy_job(source / "invalid-route-root", state_schema_version=99)
    valid_route = write_printproxy_job(source / "valid-route-root")
    assert invalid_route != valid_route
    repository = MemoryIngestionRepository()
    adapter = PrintProxyV3Adapter(
        source,
        source_instance_id="pos-synthetic",
        devices_by_target={POS_TARGET: "pos_1"},
        hmac_key=TEST_HMAC_KEY,
    )
    worker = IngestionWorker(repository, [adapter], retry_attempts=1)

    report = worker.run_once(maximum_per_adapter=10)

    assert report.discovered == 2
    assert report.imported == 1
    assert report.quarantined == 1
    assert len(repository.quarantines) == 1
    assert "state schema" in next(iter(repository.quarantines.values())).reason


def test_unknown_rch_ready_schema_is_quarantined_not_executed(tmp_path: Path) -> None:
    source = tmp_path / "rch"
    job = write_rch_job(source)
    ready = job / ".ready"
    marker = json.loads(ready.read_text(encoding="utf-8"))
    marker["schema"] = "commercialrchproxy.capture.v99"
    ready.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    repository = MemoryIngestionRepository()
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    report = IngestionWorker(repository, [adapter], retry_attempts=1).run_once(
        maximum_per_adapter=10
    )

    assert report.imported == 0
    assert report.quarantined == 1
    assert len(repository.quarantines) == 1


def test_worker_opens_and_completes_one_report_per_adapter_scan(tmp_path: Path) -> None:
    source = tmp_path / "rch"
    write_rch_job(source)
    repository = BatchTrackingRepository()
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    result = IngestionWorker(repository, [adapter], retry_attempts=1).run_once(
        maximum_per_adapter=10
    )

    assert result.imported == 1
    assert repository.started == [
        {
            "source_system": "RCHCaptureV1Adapter",
            "source_instance": "rch-synthetic",
            "source_root": source,
        }
    ]
    assert repository.completed[0][0] == "batch-1"
    assert repository.completed[0][1]["discovered"] == 1
    assert repository.completed[0][1]["imported"] == 1
