from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from retailprintguard.proxy.spool import (
    CaptureManager,
    CaptureQueueFull,
    Direction,
    SessionDescriptor,
    StorageFailurePolicy,
    recover_incomplete_spool,
    utc_now_text,
)


def _descriptor() -> SessionDescriptor:
    return SessionDescriptor(
        session_id=str(uuid4()),
        job_id=str(uuid4()),
        device_id="pos_crash",
        device_name="Synthetic crash device",
        device_type="pos",
        parser="escpos",
        client_endpoint=("127.0.0.1", 50000),
        listener_endpoint=("127.0.0.1", 9100),
        target_endpoint=("127.0.0.1", 9101),
        connected_at_utc=utc_now_text(),
        connected_monotonic_ns=time.monotonic_ns(),
    )


@pytest.mark.asyncio
async def test_unclean_partial_is_recovered_once_without_duplication(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    manager = CaptureManager(root, queue_max_events=8, fsync_each_event=True)
    assert await manager.start() == []
    descriptor = _descriptor()
    capture = await manager.open_session(descriptor, StorageFailurePolicy.CONTINUE)
    payload = b"durable-prefix\x00\xff"
    assert capture.record(
        direction=Direction.CLIENT_TO_DEVICE,
        direction_sequence=0,
        kind="data",
        captured_at_utc=utc_now_text(),
        captured_monotonic_ns=time.monotonic_ns(),
        offset=0,
        payload=payload,
        forwarded=True,
        forwarded_at_utc=utc_now_text(),
        forward_error=None,
    )

    # Stopping a writer with an open transport deliberately leaves *.partial,
    # the same on-disk state that survives process termination.
    await manager.stop()
    partials = list(root.rglob("*.partial"))
    assert len(partials) == 1
    assert not (partials[0] / ".ready").exists()

    recovered = recover_incomplete_spool(root)
    assert len(recovered) == 1
    job = recovered[0]
    assert (job / "client.raw").read_bytes() == payload
    assert (job / ".ready").is_file()
    manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "PARTIAL"
    assert manifest["close_reason"] == "recovered_after_unclean_shutdown"
    assert manifest["captured_bytes"]["client_to_device"] == len(payload)
    assert manifest["last_event_sha256"]

    assert recover_incomplete_spool(root) == []
    assert len(list(root.rglob(".ready"))) == 1


@pytest.mark.asyncio
async def test_bounded_capture_queue_never_accepts_unbounded_events(tmp_path: Path) -> None:
    manager = CaptureManager(tmp_path / "spool", queue_max_events=1, fsync_each_event=False)
    await manager.start()
    descriptor = _descriptor()
    capture = await manager.open_session(descriptor, StorageFailurePolicy.CONTINUE)

    # The exact point at which the writer drains is scheduler-dependent. A
    # finite burst must either be captured or fail closed for capture; it must
    # never expand an unbounded asyncio-side buffer.
    results = [
        capture.record(
            direction=Direction.CLIENT_TO_DEVICE,
            direction_sequence=index,
            kind="data",
            captured_at_utc=utc_now_text(),
            captured_monotonic_ns=time.monotonic_ns(),
            offset=index,
            payload=b"x",
            forwarded=True,
            forwarded_at_utc=utc_now_text(),
            forward_error=None,
        )
        for index in range(100)
    ]
    await manager.stop()
    assert not all(results)
    assert capture.failed


@pytest.mark.asyncio
async def test_recovery_marks_raw_timeline_divergence_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    manager = CaptureManager(root, queue_max_events=8, fsync_each_event=True)
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
    )
    await manager.stop()
    partial = next(root.rglob("*.partial"))
    trailing = b"-raw-without-timeline"
    with (partial / "client.raw").open("ab") as handle:
        handle.write(trailing)

    recovered = recover_incomplete_spool(root)
    assert len(recovered) == 1
    manifest = json.loads((recovered[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "PARTIAL"
    assert manifest["storage_complete"] is False
    assert manifest["captured_bytes"]["client_to_device"] == len(indexed + trailing)
    assert manifest["timeline_covered_bytes"]["client_to_device"] == len(indexed)
    assert any("timeline does not cover" in error for error in manifest["errors"])
    assert (recovered[0] / ".ready").is_file()


def test_session_open_queue_saturation_fails_immediately_and_explicitly(
    tmp_path: Path,
) -> None:
    manager = CaptureManager(tmp_path / "spool", queue_max_events=1)
    # Keep the worker stopped so queue occupancy is deterministic.  This is the
    # exact state transition exercised by open_session_nowait after start().
    manager._running = True
    manager._queue.put_nowait(object())
    descriptor = _descriptor()

    started = time.monotonic()
    with pytest.raises(CaptureQueueFull, match="before session open"):
        manager.open_session_nowait(descriptor, StorageFailurePolicy.CONTINUE)
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert descriptor.session_id not in manager._sessions
