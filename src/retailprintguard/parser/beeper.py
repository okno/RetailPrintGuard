"""Bounded, parser-side POS80 command beeper dispatch.

The ESC/POS decoder remains pure. This module provides a fast, OCR-free
classification pass plus an optional notification adapter used only by the
parser worker for POS kitchen orders.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from retailprintguard.common.domain import DocumentType
from retailprintguard.parser import escpos as escpos_parser

LOGGER = logging.getLogger("retailprintguard.parser.beeper")

# ESC ( A, payload length 5, function 97, fixed parameter n=100.
_BEEP_PREFIX = bytes((0x1B, 0x28, 0x41, 0x05, 0x00, 0x61, 0x64))

@dataclass(frozen=True, slots=True)
class _BeeperTrigger:
    enqueued_at: float


def is_complete_pos_command(payload: bytes, *, encoding: str = "cp858") -> bool:
    """Classify a complete POS command without invoking raster OCR.

    Reusing the bounded text/framing stages keeps notification semantics in
    lockstep with the versioned parser. Raster OCR is irrelevant to document
    type and must not delay the audible notification.
    """

    bounded = payload[: escpos_parser._MAX_INPUT_BYTES]
    for segment in escpos_parser._segments(bounded):
        if not segment.payload or not segment.cut_observed:
            continue
        lines, text, _warnings = escpos_parser._extract_lines(
            segment.payload,
            base_offset=segment.base_offset,
            default_encoding=encoding,
        )
        document_type, _evidence = escpos_parser._classify(text)
        semantic_lines, _metadata = escpos_parser._semantic_lines(
            lines, segment.base_offset
        )
        if any(
            line.quantity is not None and line.quantity < 0 for line in semantic_lines
        ):
            document_type = DocumentType.ORDER_CHANGE
        if document_type is DocumentType.KITCHEN_ORDER:
            return True
    return False


def _environment_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _environment_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name)
    try:
        value = default if raw is None else int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _environment_float(
    environ: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class PosBeeperConfiguration:
    """Validated parser-only configuration for the POS80 built-in beeper."""

    enabled: bool = False
    count: int = 3
    on_ms: int = 300
    off_ms: int = 200
    connect_timeout_seconds: float = 1.0
    queue_size_per_device: int = 64

    def __post_init__(self) -> None:
        if not 1 <= self.count <= 63:
            raise ValueError("beeper count must be between 1 and 63")
        for name, value in (("on_ms", self.on_ms), ("off_ms", self.off_ms)):
            if not 0 <= value <= 25_500:
                raise ValueError(f"beeper {name} must be between 0 and 25500 ms")
            if value % 100:
                raise ValueError(f"beeper {name} must be a multiple of 100 ms")
        if not 0.1 <= self.connect_timeout_seconds <= 10:
            raise ValueError("beeper connect timeout must be between 0.1 and 10 seconds")
        if not 1 <= self.queue_size_per_device <= 1_000:
            raise ValueError("beeper queue size must be between 1 and 1000")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> PosBeeperConfiguration:
        values = os.environ if environ is None else environ
        return cls(
            enabled=_environment_bool(values, "RPG_POS_BEEPER_ENABLED", False),
            count=_environment_int(
                values, "RPG_POS_BEEPER_COUNT", 3, minimum=1, maximum=63
            ),
            on_ms=_environment_int(
                values, "RPG_POS_BEEPER_ON_MS", 300, minimum=0, maximum=25_500
            ),
            off_ms=_environment_int(
                values, "RPG_POS_BEEPER_OFF_MS", 200, minimum=0, maximum=25_500
            ),
            connect_timeout_seconds=_environment_float(
                values,
                "RPG_POS_BEEPER_CONNECT_TIMEOUT_SECONDS",
                1.0,
                minimum=0.1,
                maximum=10,
            ),
            queue_size_per_device=_environment_int(
                values,
                "RPG_POS_BEEPER_QUEUE_SIZE_PER_DEVICE",
                64,
                minimum=1,
                maximum=1_000,
            ),
        )

    @property
    def command(self) -> bytes:
        return build_pos80_beep_command(self.count, self.on_ms, self.off_ms)

    @property
    def pattern_seconds(self) -> float:
        return self.count * (self.on_ms + self.off_ms) / 1000


@dataclass(frozen=True, slots=True)
class PosBeeperTarget:
    device_id: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("beeper device id cannot be empty")
        if not self.host:
            raise ValueError("beeper target host cannot be empty")
        if not 1 <= self.port <= 65_535:
            raise ValueError("beeper target port must be between 1 and 65535")


def build_pos80_beep_command(count: int = 3, on_ms: int = 300, off_ms: int = 200) -> bytes:
    """Build the documented POS80K ESC/POS beeper command without I/O."""

    configuration = PosBeeperConfiguration(count=count, on_ms=on_ms, off_ms=off_ms)
    return _BEEP_PREFIX + bytes(
        (configuration.count, configuration.on_ms // 100, configuration.off_ms // 100)
    )


def send_pos80_beep(target: PosBeeperTarget, payload: bytes, timeout: float) -> None:
    """Send one already validated command directly to the configured POS target."""

    with socket.create_connection((target.host, target.port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        with suppress(OSError):
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.sendall(payload)


class PosBeeperDispatcher:
    """Non-blocking bounded queues, isolated per POS target."""

    def __init__(
        self,
        configuration: PosBeeperConfiguration,
        targets: Sequence[PosBeeperTarget],
        *,
        sender: Callable[[PosBeeperTarget, bytes, float], None] = send_pos80_beep,
    ) -> None:
        self.configuration = configuration
        self._sender = sender
        self._stop = threading.Event()
        self._targets: dict[str, PosBeeperTarget] = {}
        self._queues: dict[str, queue.Queue[_BeeperTrigger]] = {}
        self._threads: list[threading.Thread] = []
        if len(targets) > 256:
            raise ValueError("POS beeper supports at most 256 targets")
        for target in targets:
            if target.device_id in self._targets:
                raise ValueError(f"duplicate beeper target: {target.device_id}")
            self._targets[target.device_id] = target
            self._queues[target.device_id] = queue.Queue(
                maxsize=configuration.queue_size_per_device
            )
        if configuration.enabled and not self._targets:
            raise ValueError("POS beeper is enabled but no enabled POS targets exist")
        if configuration.enabled:
            for device_id, target in self._targets.items():
                thread = threading.Thread(
                    target=self._run_device,
                    args=(target, self._queues[device_id]),
                    name=f"rpg-pos-beeper-{device_id}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def enqueue(self, device_id: str) -> bool:
        """Queue one pattern without waiting; return false when it cannot be queued."""

        if not self.configuration.enabled or self._stop.is_set():
            return False
        target_queue = self._queues.get(device_id)
        if target_queue is None:
            LOGGER.warning(
                "POS beeper target is not configured",
                extra={"event": "pos_beeper_target_missing", "device_id": device_id},
            )
            return False
        try:
            target_queue.put_nowait(_BeeperTrigger(time.monotonic()))
        except queue.Full:
            LOGGER.warning(
                "POS beeper queue is full; parser output remains valid",
                extra={"event": "pos_beeper_queue_full", "device_id": device_id},
            )
            return False
        LOGGER.info(
            "POS command beeper queued",
            extra={"event": "pos_beeper_queued", "device_id": device_id},
        )
        return True

    def drain(self, timeout: float) -> bool:
        """Wait bounded time for queued commands to reach their socket send boundary."""

        if timeout < 0:
            raise ValueError("beeper drain timeout cannot be negative")
        deadline = time.monotonic() + timeout
        while any(target_queue.unfinished_tasks for target_queue in self._queues.values()):
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        return True

    def close(self, timeout: float = 2.0) -> None:
        """Stop dispatcher threads without delaying parser shutdown indefinitely."""

        self._stop.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _run_device(
        self, target: PosBeeperTarget, target_queue: queue.Queue[_BeeperTrigger]
    ) -> None:
        while not self._stop.is_set():
            try:
                trigger = target_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._sender(
                    target,
                    self.configuration.command,
                    self.configuration.connect_timeout_seconds,
                )
                LOGGER.info(
                    "POS command beeper sent",
                    extra={
                        "event": "pos_beeper_sent",
                        "device_id": target.device_id,
                        "metrics": {
                            "bytes": len(self.configuration.command),
                            "queue_delay_ms": round(
                                max(0.0, time.monotonic() - trigger.enqueued_at) * 1000,
                                3,
                            ),
                        },
                    },
                )
            except Exception as exc:  # noqa: BLE001 - isolated notification boundary
                LOGGER.warning(
                    "POS command beeper failed; parser output remains valid",
                    extra={
                        "event": "pos_beeper_failed",
                        "device_id": target.device_id,
                        "error": type(exc).__name__,
                    },
                )
            finally:
                target_queue.task_done()


__all__ = [
    "PosBeeperConfiguration",
    "PosBeeperDispatcher",
    "PosBeeperTarget",
    "build_pos80_beep_command",
    "is_complete_pos_command",
    "send_pos80_beep",
]
