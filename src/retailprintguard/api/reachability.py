"""Asynchronous ICMP reachability cache for configured physical printers."""

from __future__ import annotations

import ipaddress
import logging
import math
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

LOGGER = logging.getLogger("retailprintguard.api.reachability")
DEFAULT_PROBE_INTERVAL_SECONDS = 10.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 1.0
_PING_BINARY = "/usr/bin/ping"
_MAX_TARGETS = 256


@dataclass(frozen=True, slots=True)
class DeviceProbeTarget:
    device_id: str
    host: str

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("probe device id cannot be empty")
        # Keep the monitor strictly on configured numeric targets: never resolve
        # DNS names and never accept shell fragments.
        ipaddress.ip_address(self.host)


@dataclass(frozen=True, slots=True)
class ReachabilitySnapshot:
    online: bool
    checked_at: datetime
    error: str | None = None


Probe = Callable[[str, float], bool]


def ping_ip(host: str, timeout_seconds: float) -> bool:
    """Run one bounded ICMP echo without a shell or observable command output."""

    address = ipaddress.ip_address(host)
    wait_seconds = max(1, math.ceil(timeout_seconds))
    family = "-4" if address.version == 4 else "-6"
    result = subprocess.run(  # noqa: S603 - fixed binary and validated IP argv
        (
            _PING_BINARY,
            family,
            "-n",
            "-c",
            "1",
            "-W",
            str(wait_seconds),
            str(address),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=timeout_seconds + 1.0,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return result.returncode == 0


class DeviceReachabilityMonitor:
    """Probe configured targets off-request and expose only a bounded cache."""

    def __init__(
        self,
        targets: Sequence[DeviceProbeTarget],
        *,
        interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        probe: Probe = ping_ip,
    ) -> None:
        if not 1 <= len(targets) <= _MAX_TARGETS:
            raise ValueError("reachability monitor requires between 1 and 256 targets")
        if not 1 <= interval_seconds <= 3600:
            raise ValueError("probe interval must be between 1 and 3600 seconds")
        if not 0.1 <= timeout_seconds <= 10:
            raise ValueError("probe timeout must be between 0.1 and 10 seconds")
        by_id = {target.device_id: target for target in targets}
        if len(by_id) != len(targets):
            raise ValueError("reachability target ids must be unique")
        self._targets = by_id
        self._interval_seconds = interval_seconds
        self._timeout_seconds = timeout_seconds
        self._probe = probe
        self._snapshots: dict[str, ReachabilitySnapshot] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start an immediate probe cycle followed by fixed 10-second cycles."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rpg-device-reachability",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(self._timeout_seconds + 1.5)
        self._thread = None

    def snapshots(self) -> Mapping[str, ReachabilitySnapshot]:
        with self._lock:
            return dict(self._snapshots)

    def counts(self) -> tuple[int, int] | None:
        snapshots = self.snapshots()
        if len(snapshots) != len(self._targets):
            return None
        online = sum(item.online for item in snapshots.values())
        return online, len(snapshots) - online

    def poll_once(self) -> None:
        """Probe every configured target concurrently and atomically refresh the cache."""

        workers = min(8, len(self._targets))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rpg-icmp") as pool:
            results = dict(
                zip(
                    self._targets,
                    pool.map(self._safe_probe, self._targets.values()),
                    strict=True,
                )
            )
        checked_at = datetime.now(UTC)
        with self._lock:
            previous = dict(self._snapshots)
            self._snapshots = {
                device_id: ReachabilitySnapshot(online, checked_at, error)
                for device_id, (online, error) in results.items()
            }
        for device_id, snapshot in self._snapshots.items():
            before = previous.get(device_id)
            if before is not None and before.online == snapshot.online:
                continue
            LOGGER.log(
                logging.INFO if snapshot.online else logging.WARNING,
                "physical printer ICMP reachability changed",
                extra={
                    "event": "device_reachability_changed",
                    "device_id": device_id,
                    "metrics": {"online": snapshot.online},
                    "error": snapshot.error,
                },
            )

    def _safe_probe(self, target: DeviceProbeTarget) -> tuple[bool, str | None]:
        try:
            return self._probe(target.host, self._timeout_seconds), None
        except Exception as exc:  # noqa: BLE001 - isolated monitoring boundary
            return False, type(exc).__name__

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_once()
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self._interval_seconds - elapsed))


__all__ = [
    "DEFAULT_PROBE_INTERVAL_SECONDS",
    "DeviceProbeTarget",
    "DeviceReachabilityMonitor",
    "ReachabilitySnapshot",
    "ping_ip",
]
