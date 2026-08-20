from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from retailprintguard.parser.beeper import (
    PosBeeperConfiguration,
    PosBeeperDispatcher,
    PosBeeperTarget,
    build_pos80_beep_command,
    is_complete_pos_command,
)
from retailprintguard.parser.spool_beeper import PosSpoolBeeperWatcher


def test_pos80_beep_command_matches_documented_bytes() -> None:
    assert build_pos80_beep_command(count=3, on_ms=300, off_ms=200) == bytes.fromhex(
        "1b 28 41 05 00 61 64 03 03 02"
    )


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        ({"RPG_POS_BEEPER_ENABLED": "maybe"}, "must be true or false"),
        ({"RPG_POS_BEEPER_COUNT": "64"}, "between 1 and 63"),
        ({"RPG_POS_BEEPER_ON_MS": "250"}, "multiple of 100"),
        ({"RPG_POS_BEEPER_OFF_MS": "-100"}, "between 0 and 25500"),
        ({"RPG_POS_BEEPER_CONNECT_TIMEOUT_SECONDS": "20"}, "between 0.1 and 10"),
        ({"RPG_POS_BEEPER_PRINTER": ""}, "cannot be empty"),
        ({"RPG_POS_BEEPER_PRINTER": "0,3"}, "between 1 and 256"),
        ({"RPG_POS_BEEPER_PRINTER": "2,2"}, "duplicate selector"),
        ({"RPG_POS_BEEPER_PRINTER": "cucina"}, "POS numbers or device ids"),
        ({"RPG_POS_BEEPER_SPOOL_POLL_SECONDS": "2"}, "between 0.05 and 1"),
    ),
)
def test_pos_beeper_environment_fails_closed(environment: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PosBeeperConfiguration.from_environment(environment)


def test_dispatcher_sends_on_the_matching_device_queue_without_network() -> None:
    delivered = Event()
    calls: list[tuple[PosBeeperTarget, bytes, float]] = []

    def sender(target: PosBeeperTarget, payload: bytes, timeout: float) -> None:
        calls.append((target, payload, timeout))
        delivered.set()

    configuration = PosBeeperConfiguration(
        enabled=True,
        count=2,
        on_ms=0,
        off_ms=0,
        connect_timeout_seconds=0.5,
        queue_size_per_device=2,
    )
    target = PosBeeperTarget("pos_synthetic", "192.0.2.20", 9100)
    dispatcher = PosBeeperDispatcher(configuration, (target,), sender=sender)
    try:
        assert dispatcher.enqueue("rch_synthetic") is False
        assert dispatcher.enqueue("pos_synthetic") is True
        assert delivered.wait(1)
        assert dispatcher.drain(1) is True
    finally:
        dispatcher.close()

    assert calls == [(target, bytes.fromhex("1b 28 41 05 00 61 64 02 00 00"), 0.5)]


def test_printer_selector_limits_beeper_to_pos_two_and_three() -> None:
    configuration = PosBeeperConfiguration.from_environment(
        {"RPG_POS_BEEPER_ENABLED": "true", "RPG_POS_BEEPER_PRINTER": "2, 3"}
    )

    configuration.validate_selection(("pos_1", "pos_2", "pos_3"))

    assert configuration.selects("pos_1") is False
    assert configuration.selects("pos_2") is True
    assert configuration.selects("pos_3") is True


def test_printer_selector_rejects_unknown_configured_pos() -> None:
    configuration = PosBeeperConfiguration.from_environment({"RPG_POS_BEEPER_PRINTER": "4"})

    with pytest.raises(ValueError, match="unknown POS printer"):
        configuration.validate_selection(("pos_1", "pos_2", "pos_3"))


def test_dispatcher_suppresses_the_same_job_across_early_and_database_paths() -> None:
    delivered = Event()
    calls: list[str] = []

    def sender(target: PosBeeperTarget, _payload: bytes, _timeout: float) -> None:
        calls.append(target.device_id)
        delivered.set()

    dispatcher = PosBeeperDispatcher(
        PosBeeperConfiguration(enabled=True, on_ms=0, off_ms=0),
        (PosBeeperTarget("pos_2", "192.0.2.20", 9100),),
        sender=sender,
    )
    try:
        assert dispatcher.enqueue("pos_2", event_id="synthetic-job") is True
        assert dispatcher.enqueue("pos_2", event_id="synthetic-job") is False
        assert delivered.wait(1)
        assert dispatcher.drain(1) is True
    finally:
        dispatcher.close()

    assert calls == ["pos_2"]


class _RecordingBeeper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def enqueue(self, device_id: str, *, event_id: str | None = None) -> bool:
        self.calls.append((device_id, event_id))
        return True


def _partial_job(root: Path, device_id: str, name: str, payload: bytes) -> Path:
    job = root / device_id / f"{name}.partial"
    job.mkdir(parents=True)
    (job / "client.raw").write_bytes(payload)
    return job


def test_spool_watcher_beeps_before_ready_and_only_after_complete_command(
    tmp_path: Path,
) -> None:
    beeper = _RecordingBeeper()
    job = _partial_job(
        tmp_path,
        "pos_2",
        "synthetic-job",
        b"\x1b@COMANDA N. C-EARLY\nTavolo: 01\n1x Pizza\n",
    )
    watcher = PosSpoolBeeperWatcher(tmp_path, ("pos_2",), beeper)

    watcher.scan_once()
    assert beeper.calls == []

    with (job / "client.raw").open("ab") as stream:
        stream.write(b"\x1dV\x00")
    watcher.scan_once()
    watcher.scan_once()

    assert not (job / ".ready").exists()
    assert beeper.calls == [("pos_2", "synthetic-job")]


def test_spool_watcher_ignores_unselected_devices_and_links(tmp_path: Path) -> None:
    beeper = _RecordingBeeper()
    payload = b"\x1b@COMANDA N. C-EARLY\n1x Pizza\n\x1dV\x00"
    _partial_job(tmp_path, "pos_1", "bar-job", payload)
    outside = tmp_path / "outside.raw"
    outside.write_bytes(payload)
    linked_job = tmp_path / "pos_2" / "linked.partial"
    linked_job.mkdir(parents=True)
    (linked_job / "client.raw").symlink_to(outside)
    watcher = PosSpoolBeeperWatcher(tmp_path, ("pos_2",), beeper)

    watcher.scan_once()

    assert beeper.calls == []


def test_complete_command_preclassification_is_ocr_free(monkeypatch) -> None:
    def forbidden_ocr(_image):
        raise AssertionError("fast beeper classification must not invoke OCR")

    monkeypatch.setattr("retailprintguard.parser.escpos._run_tesseract_ocr", forbidden_ocr)
    assert is_complete_pos_command(b"\x1b@COMANDA N. C-FAST\nTavolo: 01\n1x Pizza\n\x1dV\x00")
    assert not is_complete_pos_command(b"\x1b@PRECONTO N. P-1\nTOTALE 10,00\n\x1dV\x00")
    assert not is_complete_pos_command(b"\x1b@COMANDA N. C-PARTIAL\n1x Pizza\n")
    assert not is_complete_pos_command(b"\x1b@COMANDA N. C-CHANGE\n-1x Pizza\n\x1dV\x00")


def test_back_to_back_notifications_do_not_wait_for_previous_pattern() -> None:
    delivered = Event()
    calls: list[float] = []

    def sender(_target: PosBeeperTarget, _payload: bytes, _timeout: float) -> None:
        calls.append(len(calls))
        if len(calls) == 2:
            delivered.set()

    dispatcher = PosBeeperDispatcher(
        PosBeeperConfiguration(enabled=True, count=3, on_ms=300, off_ms=200),
        (PosBeeperTarget("pos_synthetic", "192.0.2.20", 9100),),
        sender=sender,
    )
    try:
        assert dispatcher.enqueue("pos_synthetic") is True
        assert dispatcher.enqueue("pos_synthetic") is True
        assert delivered.wait(0.5)
        assert dispatcher.drain(0.5) is True
    finally:
        dispatcher.close()

    assert len(calls) == 2


def test_dispatch_failure_isolated_from_the_dispatcher() -> None:
    attempted = Event()

    def failing_sender(_target: PosBeeperTarget, _payload: bytes, _timeout: float) -> None:
        attempted.set()
        raise TimeoutError("synthetic timeout")

    dispatcher = PosBeeperDispatcher(
        PosBeeperConfiguration(enabled=True, on_ms=0, off_ms=0),
        (PosBeeperTarget("pos_synthetic", "192.0.2.20", 9100),),
        sender=failing_sender,
    )
    try:
        assert dispatcher.enqueue("pos_synthetic") is True
        assert attempted.wait(1)
        assert dispatcher.drain(1) is True
    finally:
        dispatcher.close()


def test_disabled_dispatcher_starts_no_delivery() -> None:
    dispatcher = PosBeeperDispatcher(
        PosBeeperConfiguration(enabled=False),
        (PosBeeperTarget("pos_synthetic", "192.0.2.20", 9100),),
    )
    try:
        assert dispatcher.enqueue("pos_synthetic") is False
        assert dispatcher.drain(0) is True
    finally:
        dispatcher.close()
