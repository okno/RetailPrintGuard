from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from retailprintguard.ingestion.canonical import CanonicalCaptureV1Adapter
from retailprintguard.ingestion.dto import ArtifactRole, SourceKind, StreamDirection
from retailprintguard.ingestion.errors import SourceValidationError
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


async def _write_capture(root: Path) -> Path:
    manager = CaptureManager(root, queue_max_events=32, fsync_each_event=False)
    await manager.start()
    descriptor = _descriptor()
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
