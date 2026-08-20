from __future__ import annotations

import subprocess
from threading import Event

import pytest

from retailprintguard.api.reachability import (
    DeviceProbeTarget,
    DeviceReachabilityMonitor,
    ping_ip,
)


def test_monitor_refreshes_every_target_without_exposing_addresses() -> None:
    observed: list[str] = []

    def probe(host: str, _timeout: float) -> bool:
        observed.append(host)
        return host.endswith("20")

    monitor = DeviceReachabilityMonitor(
        (
            DeviceProbeTarget("pos_1", "192.0.2.10"),
            DeviceProbeTarget("pos_2", "192.0.2.20"),
        ),
        probe=probe,
    )

    monitor.poll_once()

    snapshots = monitor.snapshots()
    assert set(observed) == {"192.0.2.10", "192.0.2.20"}
    assert snapshots["pos_1"].online is False
    assert snapshots["pos_2"].online is True
    assert monitor.counts() == (1, 1)


def test_monitor_runs_an_immediate_background_probe_and_stops() -> None:
    called = Event()

    def probe(_host: str, _timeout: float) -> bool:
        called.set()
        return True

    monitor = DeviceReachabilityMonitor(
        (DeviceProbeTarget("pos_1", "192.0.2.20"),),
        interval_seconds=60,
        probe=probe,
    )
    monitor.start()
    try:
        assert called.wait(1)
    finally:
        monitor.close()


def test_ping_uses_fixed_binary_validated_ip_and_suppresses_output(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert ping_ip("192.0.2.20", 1.0) is True
    assert calls[0][0] == (
        "/usr/bin/ping",
        "-4",
        "-n",
        "-c",
        "1",
        "-W",
        "1",
        "192.0.2.20",
    )
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert calls[0][1]["check"] is False
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def test_probe_target_rejects_hostnames_and_shell_fragments() -> None:
    with pytest.raises(ValueError):
        DeviceProbeTarget("pos_1", "printer.example")
