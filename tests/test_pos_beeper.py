from __future__ import annotations

from threading import Event

import pytest

from retailprintguard.parser.beeper import (
    PosBeeperConfiguration,
    PosBeeperDispatcher,
    PosBeeperTarget,
    build_pos80_beep_command,
)


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
    ),
)
def test_pos_beeper_environment_fails_closed(
    environment: dict[str, str], message: str
) -> None:
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

    assert calls == [
        (target, bytes.fromhex("1b 28 41 05 00 61 64 02 00 00"), 0.5)
    ]


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
