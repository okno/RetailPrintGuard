"""Idempotent historical import built on the same production adapters."""

from __future__ import annotations

from collections.abc import Sequence

from retailprintguard.ingestion.adapters import SourceAdapter
from retailprintguard.ingestion.repository import IngestionRepository
from retailprintguard.ingestion.worker import IngestionRunReport, IngestionWorker


class HistoricalImporter:
    def __init__(
        self,
        repository: IngestionRepository,
        adapters: Sequence[SourceAdapter],
        *,
        retry_initial_seconds: float = 2.0,
        retry_max_seconds: float = 300.0,
        retry_attempts: int = 5,
    ) -> None:
        self.worker = IngestionWorker(
            repository,
            adapters,
            retry_initial_seconds=retry_initial_seconds,
            retry_max_seconds=retry_max_seconds,
            retry_attempts=retry_attempts,
        )

    def run(
        self,
        *,
        maximum_per_adapter: int = 10_000,
        maximum_passes: int = 10_000,
    ) -> IngestionRunReport:
        """Drain rotating adapter batches until a complete duplicate-only pass.

        Adapters deliberately keep source evidence immutable and do not create
        ``.imported`` files.  A final pass containing no new imports is therefore
        the idempotent completion signal from the repository.  The pass bound
        prevents an actively growing source tree from turning a one-shot import
        into an unbounded daemon.
        """

        if maximum_per_adapter < 1 or maximum_passes < 1:
            raise ValueError("historical import bounds must be positive")
        aggregate = IngestionRunReport()
        seen_candidates: set[str] = set()
        for _pass in range(maximum_passes):
            report = self.worker.run_once(maximum_per_adapter=maximum_per_adapter)
            newly_seen = report.candidate_keys - seen_candidates
            if not newly_seen:
                return aggregate
            seen_candidates.update(newly_seen)
            aggregate.merge(report)
        aggregate.errors.append(
            "historical import reached maximum_passes while new records were still arriving"
        )
        aggregate.retry_exhausted += 1
        return aggregate
