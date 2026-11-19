from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from retailprintguard.common.config import Settings
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import Device, PrintJob, ProxySession, RawPayload
from retailprintguard.db.repository import SqlAlchemyIngestionRepository, _sync_devices
from retailprintguard.ingestion.canonical import CanonicalCaptureV1Adapter
from retailprintguard.ingestion.dto import ArtifactRole, SourceKind, StreamDirection
from retailprintguard.ingestion.errors import SourceValidationError
from retailprintguard.ingestion.main import _parser, build_adapters
from retailprintguard.ingestion.worker import IngestionWorker
from retailprintguard.proxy.spool import (
    CaptureManager,
    Direction,
    SessionCloseSummary,
    SessionDescriptor,
    StorageFailurePolicy,
    recover_incomplete_spool,
    utc_now_text,
)

TARGET = ("192.0.2.31", 9100)


def _descriptor() -> SessionDescriptor:
    return SessionDescriptor(
        session_id=str(uuid4()),
        job_id=str(uuid4()),
        device_id="pos_1",
        device_name="POS sintetica",
        device_type="pos",
        parser="escpos",
        client_endpoint=("192.0.2.10", 50123),
        listener_endpoint=("192.0.2.20", 9100),
        target_endpoint=TARGET,
        connected_at_utc=utc_now_text(),
        connected_monotonic_ns=time.monotonic_ns(),
    )


async def _write_capture(
    root: Path, *, device_type: str = "pos", parser: str = "escpos",
    target_port: int = 9100, device_id: str = "pos_1", target_ip: str = TARGET[0],
) -> Path:
    manager = CaptureManager(root, queue_max_events=32, fsync_each_event=False)
    await manager.start()
    descriptor = replace(
        _descriptor(), device_type=device_type, parser=parser, device_id=device_id,
        listener_endpoint=("192.0.2.20", target_port),
        target_endpoint=(target_ip, target_port),
    )
    capture = await manager.open_session(descriptor, StorageFailurePolicy.CONTINUE)
    client = b"ordine-sintetico\x1dV\x00"
    response = b"\x10\x00"
    events = (
        (Direction.CLIENT_TO_DEVICE, 0, "data", 0, client),
        (Direction.DEVICE_TO_CLIENT, 0, "data", 0, response),
        (Direction.CLIENT_TO_DEVICE, 1, "eof", len(client), b""),
        (Direction.DEVICE_TO_CLIENT, 1, "eof", len(response), b""),
    )
    for observed, (direction, direction_sequence, kind, offset, payload) in enumerate(events):
        assert capture.record(
            direction=direction,
            direction_sequence=direction_sequence,
            kind=kind,
            captured_at_utc=utc_now_text(),
            captured_monotonic_ns=time.monotonic_ns(),
            offset=offset,
            payload=payload,
            forwarded=True,
            forwarded_at_utc=utc_now_text(),
            forward_error=None,
            observed_sequence=observed,
        )
    published = await capture.finalize(
        SessionCloseSummary(
            closed_at_utc=utc_now_text(),
            closed_monotonic_ns=time.monotonic_ns(),
            close_reason="both_directions_eof",
            transport_complete=True,
            observed_bytes={
                "client_to_device": len(client),
                "device_to_client": len(response),
            },
            observed_chunks={"client_to_device": 1, "device_to_client": 1},
        )
    )
    await manager.stop()
    assert published is not None
    return published.job_path


@pytest.mark.asyncio
async def test_readdressed_device_keeps_historical_spool_valid_without_rewriting_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    first_job = await _write_capture(root)
    configuration = {
        "spool_root": "/srv/synthetic-migration/spool",
        "archive_root": "/srv/synthetic-migration/archive",
        "log_root": "/srv/synthetic-migration/log",
        "devices": [{
            "id": "pos_1", "name": "Synthetic migrated device", "type": "pos",
            "parser": "escpos", "listen_ip": "192.0.2.20", "listen_port": 9100,
            "target_ip": TARGET[0], "target_port": 9100,
            "allowed_networks": ["192.0.2.0/24"],
        }],
    }
    args = _parser().parse_args(
        ["--config", "synthetic.yaml", "--canonical-root", str(root)]
    )
    original = Settings.model_validate(configuration)
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    try:
        _sync_devices(factory, original)
        repository = SqlAlchemyIngestionRepository(factory, spool_root=root)
        first = IngestionWorker(
            repository, build_adapters(args, original), retry_attempts=1
        ).run_once(maximum_per_adapter=10)
        assert first.imported == 1
        pending_job = await _write_capture(root)
        original_bytes = {
            path: path.read_bytes()
            for job in (first_job, pending_job)
            for path in job.iterdir() if path.is_file()
        }

        configuration["devices"][0]["target_ip"] = "198.51.100.31"
        without_history = Settings.model_validate(configuration)
        unknown_old_target = build_adapters(args, without_history)[0]
        old_candidate = next(
            candidate for candidate in unknown_old_target.discover(maximum=10)
            if candidate.source_path == first_job
        )
        with pytest.raises(SourceValidationError, match="not mapped"):
            unknown_old_target.load(old_candidate)

        configuration["devices"][0]["historical_targets"] = [
            {"ip": TARGET[0], "port": TARGET[1]}
        ]
        migrated = Settings.model_validate(configuration)
        _sync_devices(factory, migrated)
        await _write_capture(root, target_ip="198.51.100.31")
        worker = IngestionWorker(
            repository, build_adapters(args, migrated), retry_attempts=1
        )
        after_migration = worker.run_once(maximum_per_adapter=10)
        assert (
            after_migration.imported, after_migration.duplicates, after_migration.quarantined
        ) == (2, 1, 0)
        assert worker.run_once(maximum_per_adapter=10).duplicates == 3
        assert {path: path.read_bytes() for path in original_bytes} == original_bytes
        with factory() as session:
            devices = session.scalars(select(Device)).all()
            assert len(devices) == 1
            assert devices[0].external_id == "pos_1"
            assert devices[0].target_ip == "198.51.100.31"
            jobs = session.scalars(select(PrintJob)).all()
            assert len(jobs) == 3
            assert {job.device_id for job in jobs} == {devices[0].id}
            sessions = session.scalars(select(ProxySession)).all()
            assert sorted(item.target_ip for item in sessions) == [
                TARGET[0], TARGET[0], "198.51.100.31"
            ]
            assert {item.target_port for item in sessions} == {9100}
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_id", "target_ip", "message"),
    [
        ("unconfigured_device", TARGET[0], "device id does not match configured endpoint"),
        ("pos_1", "198.51.100.99", "not mapped"),
    ],
)
async def test_historical_endpoint_allowlist_still_rejects_unknown_or_mismatched_identity(
    tmp_path: Path, device_id: str, target_ip: str, message: str
) -> None:
    root = tmp_path / "spool"
    await _write_capture(root, device_id=device_id, target_ip=target_ip)
    adapter = CanonicalCaptureV1Adapter(
        root, source_instance_id="synthetic-history",
        devices_by_target={TARGET: "pos_1", ("198.51.100.31", 9100): "pos_1"},
    )
    with pytest.raises(SourceValidationError, match=message):
        adapter.load(adapter.discover(maximum=1)[0])


@pytest.mark.asyncio
async def test_epson_both_configured_ports_import_into_one_device_with_actual_endpoints(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    jobs = [
        await _write_capture(
            root, device_type="epson", parser="opaque_raw", target_port=port,
            device_id="epson_one",
        )
        for port in (9100, 80)
    ]
    settings = Settings.model_validate(
        {
            "spool_root": "/srv/synthetic-epson/spool",
            "archive_root": "/srv/synthetic-epson/archive",
            "log_root": "/srv/synthetic-epson/log",
            "devices": [{
                "id": "epson_one", "name": "Synthetic Epson", "type": "epson",
                "parser": "opaque_raw", "listen_ip": "192.0.2.20", "listen_port": 9100,
                "target_ip": TARGET[0], "target_port": 9100, "additional_ports": [80],
                "allowed_networks": ["192.0.2.0/24"],
            }],
        }
    )
    args = _parser().parse_args(
        ["--config", "synthetic.yaml", "--canonical-root", str(root)]
    )
    adapters = build_adapters(args, settings)
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    try:
        _sync_devices(factory, settings)
        repository = SqlAlchemyIngestionRepository(factory, spool_root=root)
        worker = IngestionWorker(repository, adapters, retry_attempts=1)
        first = worker.run_once(maximum_per_adapter=10)
        second = worker.run_once(maximum_per_adapter=10)
        assert (first.imported, first.quarantined, first.retry_exhausted) == (2, 0, 0)
        assert second.duplicates == 2
        with factory() as session:
            devices = session.scalars(select(Device)).all()
            assert len(devices) == 1
            device = devices[0]
            assert device.external_id == "epson_one"
            assert device.listen_port == device.target_port == 9100
            sessions = session.scalars(select(ProxySession)).all()
            assert len(sessions) == 2
            assert {item.device_id for item in sessions} == {device.id}
            assert {(item.listen_port, item.target_port) for item in sessions} == {
                (9100, 9100), (80, 80),
            }
            assert {(item.listen_ip, item.target_ip) for item in sessions} == {
                ("192.0.2.20", TARGET[0])
            }
            imported_jobs = session.scalars(select(PrintJob)).all()
            assert len(imported_jobs) == 2
            assert {job.device_id for job in imported_jobs} == {device.id}
            raw = session.scalars(
                select(RawPayload).where(
                    RawPayload.artifact_role.in_(["REQUEST_RAW", "RESPONSE_RAW"])
                )
            ).all()
            assert len(raw) == 4
            assert {item.device_id for item in raw} == {device.id}
            for item in raw:
                filename = "client.raw" if item.artifact_role == "REQUEST_RAW" else "device.raw"
                assert item.payload == (jobs[0] / filename).read_bytes()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_epson_capture_preserves_both_directions_without_rch_semantics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    job_path = await _write_capture(root, device_type="epson", parser="opaque_raw")
    adapter = CanonicalCaptureV1Adapter(
        root, source_instance_id="epson-synthetic", devices_by_target={TARGET: "pos_1"}
    )
    envelope = adapter.load(adapter.discover(maximum=1)[0])
    by_role = {item.role: item for item in envelope.artifacts}
    assert by_role[ArtifactRole.REQUEST_RAW].content == (job_path / "client.raw").read_bytes()
    assert by_role[ArtifactRole.RESPONSE_RAW].content == (job_path / "device.raw").read_bytes()


@pytest.mark.asyncio
async def test_epson_capture_rejects_rch_parser(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    await _write_capture(root, device_type="epson", parser="rch_observed")
    adapter = CanonicalCaptureV1Adapter(
        root, source_instance_id="epson-synthetic", devices_by_target={TARGET: "pos_1"}
    )
    with pytest.raises(SourceValidationError, match="Epson fiscal captures require"):
        adapter.load(adapter.discover(maximum=1)[0])


@pytest.mark.asyncio
async def test_native_spool_import_validates_both_raw_and_hash_chained_timeline(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    await _write_capture(root)
    adapter = CanonicalCaptureV1Adapter(
        root,
        source_instance_id="rpg-synthetic",
        devices_by_target={TARGET: "pos_1"},
    )

    candidates = adapter.discover(maximum=10)
    assert len(candidates) == 1
    envelope = adapter.load(candidates[0])

    assert envelope.source_kind is SourceKind.RETAILPRINTGUARD_CAPTURE_V1
    assert envelope.device_id == "pos_1"
    assert envelope.complete is True
    assert [item.direction for item in envelope.chunks[:2]] == [
        StreamDirection.CLIENT_TO_DEVICE,
        StreamDirection.DEVICE_TO_CLIENT,
    ]
    assert [item.event_kind for item in envelope.chunks] == ["data", "data", "eof", "eof"]
    assert [item.observed_sequence for item in envelope.chunks] == [0, 1, 2, 3]
    assert {artifact.role for artifact in envelope.artifacts} == {
        ArtifactRole.REQUEST_RAW,
        ArtifactRole.RESPONSE_RAW,
        ArtifactRole.RECEIVE_TIMELINE,
        ArtifactRole.SESSION_DESCRIPTOR,
        ArtifactRole.CAPTURE_MANIFEST,
        ArtifactRole.CAPTURE_READY_MARKER,
    }


@pytest.mark.asyncio
async def test_native_spool_rejects_tampered_raw_even_with_ready_manifest_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    job = await _write_capture(root)
    raw_path = job / "device.raw"
    raw_path.write_bytes(raw_path.read_bytes() + b"tamper")
    adapter = CanonicalCaptureV1Adapter(
        root,
        source_instance_id="rpg-synthetic",
        devices_by_target={TARGET: "pos_1"},
    )

    with pytest.raises(SourceValidationError, match="size mismatch|SHA-256 mismatch"):
        adapter.load(adapter.discover(maximum=10)[0])


@pytest.mark.asyncio
async def test_native_spool_rejects_rewritten_event_chain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    job = await _write_capture(root)
    timeline_path = job / "timeline.jsonl"
    records = [json.loads(line) for line in timeline_path.read_text(encoding="utf-8").splitlines()]
    records[1]["previous_event_sha256"] = "0" * 64
    body = dict(records[1])
    body.pop("event_sha256")
    records[1]["event_sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    timeline_raw = b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
        for record in records
    )
    timeline_path.write_bytes(timeline_raw)
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["timeline.jsonl"] = {
        "name": "timeline.jsonl",
        "size": len(timeline_raw),
        "sha256": hashlib.sha256(timeline_raw).hexdigest(),
    }
    manifest_raw = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    manifest_path.write_bytes(manifest_raw)
    ready_path = job / ".ready"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    ready["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    ready_path.write_bytes(
        json.dumps(ready, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    adapter = CanonicalCaptureV1Adapter(
        root,
        source_instance_id="rpg-synthetic",
        devices_by_target={TARGET: "pos_1"},
    )

    with pytest.raises(SourceValidationError, match="hash-chain mismatch"):
        adapter.load(adapter.discover(maximum=10)[0])


def test_discovery_observes_ready_published_after_directory_rename(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    job = root / "pos_1" / "synthetic-job"
    job.mkdir(parents=True)
    adapter = CanonicalCaptureV1Adapter(
        root,
        source_instance_id="rpg-synthetic",
        devices_by_target={TARGET: "pos_1"},
    )

    # The canonical publisher renames the directory first, then publishes
    # .ready inside it. The device-directory mtime need not change twice.
    assert adapter.discover(maximum=10) == ()
    (job / ".ready").write_text("{}\n", encoding="utf-8")

    discovered = adapter.discover(maximum=10)
    assert len(discovered) == 1
    assert discovered[0].source_path == job


@pytest.mark.asyncio
async def test_native_spool_accepts_full_duplex_completion_order_under_backpressure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    manager = CaptureManager(root, queue_max_events=16, fsync_each_event=False)
    await manager.start()
    descriptor = _descriptor()
    capture = await manager.open_session(descriptor, StorageFailurePolicy.CONTINUE)
    reverse = b"fast-reverse"
    request = b"slow-request"
    # observed_sequence records when read() completed.  The persisted sequence
    # records when forwarding/drain completed, which may be the opposite order
    # under independent full-duplex backpressure.
    assert capture.record(
        direction=Direction.DEVICE_TO_CLIENT,
        direction_sequence=0,
        kind="data",
        captured_at_utc=utc_now_text(),
        captured_monotonic_ns=time.monotonic_ns(),
        offset=0,
        payload=reverse,
        forwarded=True,
        forwarded_at_utc=utc_now_text(),
        forward_error=None,
        observed_sequence=1,
    )
    assert capture.record(
        direction=Direction.CLIENT_TO_DEVICE,
        direction_sequence=0,
        kind="data",
        captured_at_utc=utc_now_text(),
        captured_monotonic_ns=time.monotonic_ns(),
        offset=0,
        payload=request,
        forwarded=True,
        forwarded_at_utc=utc_now_text(),
        forward_error=None,
        observed_sequence=0,
    )
    published = await capture.finalize(
        SessionCloseSummary(
            closed_at_utc=utc_now_text(),
            closed_monotonic_ns=time.monotonic_ns(),
            close_reason="clean_bidirectional_eof",
            transport_complete=True,
            observed_bytes={
                "client_to_device": len(request),
                "device_to_client": len(reverse),
            },
            observed_chunks={"client_to_device": 1, "device_to_client": 1},
        )
    )
    await manager.stop()
    assert published is not None

    adapter = CanonicalCaptureV1Adapter(
        root,
        source_instance_id="rpg-backpressure",
        devices_by_target={TARGET: "pos_1"},
    )
    envelope = adapter.load(adapter.discover(maximum=10)[0])
    assert envelope.complete is True
    assert [chunk.observed_sequence for chunk in envelope.chunks] == [1, 0]
    assert [chunk.direction for chunk in envelope.chunks] == [
        StreamDirection.DEVICE_TO_CLIENT,
        StreamDirection.CLIENT_TO_DEVICE,
    ]


@pytest.mark.asyncio
async def test_recovered_trailing_raw_is_imported_as_explicit_partial_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    manager = CaptureManager(root, queue_max_events=16, fsync_each_event=True)
    await manager.start()
    descriptor = _descriptor()
    capture = await manager.open_session(descriptor, StorageFailurePolicy.CONTINUE)
    indexed = b"indexed-prefix"
    assert capture.record(
        direction=Direction.CLIENT_TO_DEVICE,
        direction_sequence=0,
        kind="data",
        captured_at_utc=utc_now_text(),
        captured_monotonic_ns=time.monotonic_ns(),
        offset=0,
        payload=indexed,
        forwarded=True,
        forwarded_at_utc=utc_now_text(),
        forward_error=None,
        observed_sequence=0,
    )
    await manager.stop()
    partial = next(root.rglob("*.partial"))
    trailing = b"-unindexed-after-power-loss"
    with (partial / "client.raw").open("ab") as handle:
        handle.write(trailing)
    recovered = recover_incomplete_spool(root)
    assert len(recovered) == 1

    adapter = CanonicalCaptureV1Adapter(
        root,
        source_instance_id="rpg-recovery",
        devices_by_target={TARGET: "pos_1"},
    )
    envelope = adapter.load(adapter.discover(maximum=10)[0])
    assert envelope.status == "PARTIAL"
    assert envelope.complete is False
    assert len(envelope.chunks) == 1
    assert envelope.chunks[0].byte_count == len(indexed)
    request = next(
        artifact for artifact in envelope.artifacts if artifact.role is ArtifactRole.REQUEST_RAW
    )
    assert request.content == indexed + trailing
    assert request.complete is False
    assert any("timeline does not cover" in warning for warning in envelope.warnings)
