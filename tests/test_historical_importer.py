from __future__ import annotations

import json
from pathlib import Path

from ingestion_fixtures import RCH_TARGET, write_rch_job

from retailprintguard.importer.historical import HistoricalImporter
from retailprintguard.ingestion.main import run_cli
from retailprintguard.ingestion.rch import RCHCaptureV1Adapter
from retailprintguard.ingestion.repository import MemoryIngestionRepository


def test_historical_importer_reuses_atomic_idempotency_contract(tmp_path: Path) -> None:
    source = tmp_path / "rch"
    write_rch_job(source)
    repository = MemoryIngestionRepository()
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-history",
        devices_by_target={RCH_TARGET: "rch_1"},
    )
    importer = HistoricalImporter(
        repository,
        [adapter],
        retry_initial_seconds=0.01,
        retry_max_seconds=0.01,
        retry_attempts=1,
    )

    first = importer.run(maximum_per_adapter=10)
    repeated = importer.run(maximum_per_adapter=10)

    assert first.imported == 1
    assert repeated.duplicates == 1
    assert len(repository.imports) == 1


def test_historical_cli_validate_only_needs_no_database(
    tmp_path: Path,
    capsys: object,
) -> None:
    source = tmp_path / "rch"
    write_rch_job(source)
    config = Path(__file__).parents[1] / "config" / "retailprintguard.example.yaml"

    exit_code = run_cli(
        [
            "--config",
            str(config),
            "--rch-root",
            str(source),
            "--validate-only",
            "--json",
        ],
        historical=True,
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    report = json.loads(captured.out)
    assert exit_code == 0
    assert report["imported"] == 1
    assert report["quarantined"] == 0
