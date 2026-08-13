"""Generic byte-exact TCP relays for POS and RCH devices.

This module has no parser or database dependency.  It connects to the physical
device before it starts reading from the client, relays both directions with
normal asyncio backpressure, and submits evidence to a bounded non-blocking
capture queue only after each forwarding attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from dataclasses import dataclass, field
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any
from uuid import uuid4

from retailprintguard.common.config import DeviceConfig, Settings
from retailprintguard.proxy.spool import (
    CaptureError,
    CaptureManager,
    CaptureSession,
    Direction,
    PublishedCapture,
    SessionCloseSummary,
    SessionDescriptor,
    StorageFailurePolicy,
    utc_now_text,
)

LOGGER = logging.getLogger("retailprintguard.proxy")


class RelayError(RuntimeError):
    """A transport session cannot continue safely."""


class CaptureAbort(RelayError):
    """Capture policy requires the transport session to stop."""


@dataclass(slots=True)
class _RelayCounters:
    observed_events: int = 0
    observed_bytes: dict[str, int] = field(
        default_factory=lambda: {direction.value: 0 for direction in Direction}
    )
    observed_chunks: dict[str, int] = field(
        default_factory=lambda: {direction.value: 0 for direction in Direction}
    )
    direction_events: dict[str, int] = field(
        default_factory=lambda: {direction.value: 0 for direction in Direction}
    )

    def next_data(self, direction: Direction, length: int) -> tuple[int, int, int]:
        key = direction.value
        offset = self.observed_bytes[key]
        direction_sequence = self.direction_events[key]
        observed_sequence = self.observed_events
        self.observed_bytes[key] += length
        self.observed_chunks[key] += 1
        self.direction_events[key] += 1
        self.observed_events += 1
        return offset, direction_sequence, observed_sequence

    def next_eof(self, direction: Direction) -> tuple[int, int, int]:
        key = direction.value
        offset = self.observed_bytes[key]
        direction_sequence = self.direction_events[key]
        observed_sequence = self.observed_events
        self.direction_events[key] += 1
        self.observed_events += 1
        return offset, direction_sequence, observed_sequence


@dataclass(frozen=True, slots=True)
class _PumpOutcome:
    direction: Direction
    clean_eof: bool


@dataclass(frozen=True, slots=True)
class _SessionOutcome:
    complete: bool
    reason: str
    errors: tuple[str, ...]


@dataclass(slots=True)
class _ActivityClock:
    last_activity: float = field(default_factory=time.monotonic)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def touch(self) -> None:
        self.last_activity = time.monotonic()
        self.changed.set()

    async def wait_until_idle(self, timeout: float) -> None:
        while True:
            remaining = timeout - (time.monotonic() - self.last_activity)
            if remaining <= 0:
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=remaining)
            except TimeoutError:
                return


class RelayService:
    """Run one independent listener per enabled configured device."""

    def __init__(self, settings: Settings, capture_manager: CaptureManager | None = None) -> None:
        self.settings = settings
        self.capture_manager = capture_manager or CaptureManager(
            settings.spool_root,
            queue_max_events=settings.proxy.capture_queue_max_events,
            fsync_each_event=settings.proxy.fsync_each_event,
            recover_device_ids=frozenset(
                device.id for device in settings.devices if device.enabled
            ),
        )
        self._servers: dict[str, asyncio.AbstractServer] = {}
        self._target_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._active_sessions: set[asyncio.Task[Any]] = set()
        self._capture_finalizers: set[asyncio.Task[Any]] = set()
        self._started = False
        self.published_jobs: list[PublishedCapture] = []
        self.recovered_jobs: list[Path] = []

    @property
    def listener_endpoints(self) -> dict[str, tuple[str, int]]:
        endpoints: dict[str, tuple[str, int]] = {}
        for device_id, server in self._servers.items():
            sockets = server.sockets or ()
            if not sockets:
                continue
            host, port = sockets[0].getsockname()[:2]
            endpoints[device_id] = (str(host), int(port))
        return endpoints

    async def start(self) -> None:
        if self._started:
            return
        try:
            self.recovered_jobs = await self.capture_manager.start()
        except (CaptureError, OSError) as exc:
            if self.settings.proxy.storage_failure_policy == StorageFailurePolicy.ABORT.value:
                raise
            LOGGER.critical(
                "capture_start_failed",
                extra={
                    "event": "capture_start_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            self.recovered_jobs = []
        started: list[asyncio.AbstractServer] = []
        try:
            for device in self.settings.devices:
                if not device.enabled:
                    continue
                target = (str(device.target_ip), device.target_port)
                if target in self._target_locks:
                    raise RelayError(f"duplicate enabled target endpoint: {target[0]}:{target[1]}")
                self._target_locks[target] = asyncio.Lock()
                server = await asyncio.start_server(
                    lambda reader, writer, route=device: self._handle_client(route, reader, writer),
                    host=str(device.listen_ip),
                    port=device.listen_port,
                    start_serving=True,
                )
                self._servers[device.id] = server
                started.append(server)
                self._log(
                    logging.INFO,
                    "listener_started",
                    device,
                    listen=f"{device.listen_ip}:{device.listen_port}",
                    target=f"{device.target_ip}:{device.target_port}",
                )
        except BaseException:
            for server in started:
                server.close()
            await asyncio.gather(*(server.wait_closed() for server in started))
            self._servers.clear()
            self._target_locks.clear()
            await self.capture_manager.stop()
            raise
        self._started = True

    async def serve_forever(self) -> None:
        if not self._started:
            await self.start()
        await asyncio.gather(*(server.serve_forever() for server in self._servers.values()))

    async def stop(self) -> None:
        if not self._started:
            return
        for server in self._servers.values():
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in self._servers.values()))
        self._servers.clear()

        current = asyncio.current_task()
        active = [task for task in self._active_sessions if task is not current]
        if active:
            done, pending = await asyncio.wait(
                active,
                timeout=self.settings.proxy.shutdown_grace_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
        finalizers = list(self._capture_finalizers)
        if finalizers:
            done, pending = await asyncio.wait(
                finalizers,
                timeout=self.settings.proxy.shutdown_grace_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
        await self.capture_manager.stop()
        self._target_locks.clear()
        self._started = False

    async def _handle_client(
        self,
        device: DeviceConfig,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_sessions.add(task)
        try:
            if len(self._active_sessions) > self.settings.proxy.max_connections:
                self._log(logging.WARNING, "connection_limit_rejected", device)
                self._abort_writer(client_writer)
                return
            peer = _endpoint(client_writer.get_extra_info("peername"))
            if peer is None or not _client_allowed(device, peer[0]):
                self._log(
                    logging.WARNING,
                    "acl_rejected",
                    device,
                    client=peer[0] if peer else "unknown",
                )
                self._abort_writer(client_writer)
                return

            target = (str(device.target_ip), device.target_port)
            target_lock = self._target_locks[target]
            if target_lock.locked():
                self._log(logging.WARNING, "target_busy_rejected", device, client=peer[0])
                self._abort_writer(client_writer)
                return
            await target_lock.acquire()
            try:
                await self._relay_locked(device, peer, client_reader, client_writer)
            finally:
                target_lock.release()
        finally:
            await _close_writer(client_writer)
            if task is not None:
                self._active_sessions.discard(task)

    async def _relay_locked(
        self,
        device: DeviceConfig,
        peer: tuple[str, int],
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            device_reader, device_writer = await asyncio.wait_for(
                asyncio.open_connection(str(device.target_ip), device.target_port),
                timeout=self.settings.proxy.connect_timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            self._log(
                logging.ERROR,
                "upstream_connect_failed",
                device,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        # No client read occurs before the physical-device connection above.
        now = utc_now_text()
        descriptor = SessionDescriptor(
            session_id=str(uuid4()),
            job_id=str(uuid4()),
            device_id=device.id,
            device_name=device.name,
            device_type=device.type.value,
            parser=device.parser.value,
            client_endpoint=peer,
            listener_endpoint=(str(device.listen_ip), device.listen_port),
            target_endpoint=(str(device.target_ip), device.target_port),
            connected_at_utc=now,
            connected_monotonic_ns=time.monotonic_ns(),
        )
        policy = StorageFailurePolicy(self.settings.proxy.storage_failure_policy)
        try:
            capture = await self.capture_manager.open_session(descriptor, policy)
        except (CaptureError, OSError) as exc:
            message = f"capture initialization failed: {type(exc).__name__}: {exc}"
            self._log(logging.CRITICAL, "capture_open_failed", device, error=message)
            if policy is StorageFailurePolicy.ABORT:
                await _close_writer(device_writer)
                return
            capture = self.capture_manager.disabled_session(descriptor, policy, message)

        counters = _RelayCounters()
        outcome = _SessionOutcome(False, "internal_error", ())
        try:
            outcome = await self._run_full_duplex(
                client_reader,
                client_writer,
                device_reader,
                device_writer,
                capture,
                counters,
            )
        except asyncio.CancelledError:
            outcome = _SessionOutcome(False, "service_shutdown", ("session task cancelled",))
            raise
        finally:
            await _close_writer(device_writer)

            close_summary = SessionCloseSummary(
                closed_at_utc=utc_now_text(),
                closed_monotonic_ns=time.monotonic_ns(),
                close_reason=outcome.reason,
                transport_complete=outcome.complete,
                observed_bytes=dict(counters.observed_bytes),
                observed_chunks=dict(counters.observed_chunks),
                transport_errors=outcome.errors,
            )
            finalizer = asyncio.create_task(
                self._finalize_capture(device, descriptor, capture, close_summary),
                name=f"capture-finalize-{descriptor.job_id}",
            )
            self._capture_finalizers.add(finalizer)
            finalizer.add_done_callback(self._capture_finalizers.discard)
            self._log(
                logging.INFO if outcome.complete else logging.WARNING,
                "session_closed",
                device,
                session_id=descriptor.session_id,
                job_id=descriptor.job_id,
                reason=outcome.reason,
                client_bytes=counters.observed_bytes[Direction.CLIENT_TO_DEVICE.value],
                device_bytes=counters.observed_bytes[Direction.DEVICE_TO_CLIENT.value],
            )

    async def _finalize_capture(
        self,
        device: DeviceConfig,
        descriptor: SessionDescriptor,
        capture: CaptureSession,
        close_summary: SessionCloseSummary,
    ) -> None:
        try:
            published = await capture.finalize(close_summary)
        except (CaptureError, OSError) as exc:
            self._log(
                logging.CRITICAL,
                "capture_finalize_failed",
                device,
                session_id=descriptor.session_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            if published is not None:
                self.published_jobs.append(published)

    async def _run_full_duplex(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        device_reader: asyncio.StreamReader,
        device_writer: asyncio.StreamWriter,
        capture: CaptureSession,
        counters: _RelayCounters,
    ) -> _SessionOutcome:
        activity = _ActivityClock()
        pumps = {
            asyncio.create_task(
                self._pump(
                    client_reader,
                    device_writer,
                    Direction.CLIENT_TO_DEVICE,
                    capture,
                    counters,
                    activity,
                ),
                name="client-to-device",
            ),
            asyncio.create_task(
                self._pump(
                    device_reader,
                    client_writer,
                    Direction.DEVICE_TO_CLIENT,
                    capture,
                    counters,
                    activity,
                ),
                name="device-to-client",
            ),
        }
        idle_watcher = asyncio.create_task(
            activity.wait_until_idle(self.settings.proxy.session_idle_timeout_seconds),
            name="session-idle-timeout",
        )
        failure_watcher: asyncio.Task[str] | None = None
        if capture.policy is StorageFailurePolicy.ABORT:
            failure_watcher = asyncio.create_task(capture.wait_failed(), name="capture-failure")

        watched: set[asyncio.Task[Any]] = {*pumps, idle_watcher}
        if failure_watcher is not None:
            watched.add(failure_watcher)
        try:
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if idle_watcher in done:
                await _cancel_tasks(pumps)
                return _SessionOutcome(
                    False,
                    "session_idle_timeout",
                    ("no traffic in either direction before the configured timeout",),
                )
            if failure_watcher is not None and failure_watcher in done:
                error = failure_watcher.result()
                await _cancel_tasks(pumps)
                return _SessionOutcome(False, "storage_failure", (error,))

            error = _first_task_error(done.intersection(pumps))
            if error is not None:
                await _cancel_tasks(pumps.difference(done))
                return _SessionOutcome(False, "transport_error", (error,))

            remaining = {pump for pump in pumps if not pump.done()}
            if remaining:
                tail_watch: set[asyncio.Task[Any]] = set(remaining)
                tail_watch.add(idle_watcher)
                if failure_watcher is not None:
                    tail_watch.add(failure_watcher)
                done_tail, pending_tail = await asyncio.wait(
                    tail_watch,
                    timeout=self.settings.proxy.response_tail_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if idle_watcher in done_tail:
                    await _cancel_tasks(remaining)
                    return _SessionOutcome(
                        False,
                        "session_idle_timeout",
                        ("no traffic in either direction before the configured timeout",),
                    )
                if failure_watcher is not None and failure_watcher in done_tail:
                    error = failure_watcher.result()
                    await _cancel_tasks(remaining)
                    return _SessionOutcome(False, "storage_failure", (error,))
                unfinished_pumps = {task for task in pending_tail if task in pumps}
                if unfinished_pumps:
                    await _cancel_tasks(unfinished_pumps)
                    return _SessionOutcome(
                        False,
                        "half_close_tail_timeout",
                        ("opposite direction did not reach EOF before the configured timeout",),
                    )
                error = _first_task_error({task for task in done_tail if task in pumps})
                if error is not None:
                    return _SessionOutcome(False, "transport_error", (error,))

            return _SessionOutcome(True, "clean_bidirectional_eof", ())
        finally:
            await _cancel_tasks({task for task in pumps if not task.done()})
            await _cancel_tasks({idle_watcher})
            if failure_watcher is not None:
                await _cancel_tasks({failure_watcher})

    async def _pump(
        self,
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
        direction: Direction,
        capture: CaptureSession,
        counters: _RelayCounters,
        activity: _ActivityClock,
    ) -> _PumpOutcome:
        while True:
            payload = await source.read(self.settings.proxy.read_chunk_bytes)
            captured_at = utc_now_text()
            captured_monotonic = time.monotonic_ns()
            activity.touch()
            if not payload:
                offset, direction_sequence, observed_sequence = counters.next_eof(direction)
                forwarded = True
                forward_error: str | None = None
                try:
                    await _forward_eof(destination, self.settings.proxy.forward_timeout_seconds)
                except (TimeoutError, OSError, RuntimeError) as exc:
                    forwarded = False
                    forward_error = f"{type(exc).__name__}: {exc}"
                accepted = capture.record(
                    direction=direction,
                    direction_sequence=direction_sequence,
                    kind="eof",
                    captured_at_utc=captured_at,
                    captured_monotonic_ns=captured_monotonic,
                    offset=offset,
                    payload=b"",
                    forwarded=forwarded,
                    forwarded_at_utc=utc_now_text(),
                    forward_error=forward_error,
                    observed_sequence=observed_sequence,
                )
                if (not accepted and capture.policy is StorageFailurePolicy.ABORT) or not forwarded:
                    raise CaptureAbort(forward_error or capture.failure_message)
                return _PumpOutcome(direction, True)

            offset, direction_sequence, observed_sequence = counters.next_data(
                direction, len(payload)
            )
            forwarded = True
            forward_error = None
            try:
                destination.write(payload)
                await asyncio.wait_for(
                    destination.drain(),
                    timeout=self.settings.proxy.forward_timeout_seconds,
                )
            except (TimeoutError, OSError, RuntimeError) as exc:
                forwarded = False
                forward_error = f"{type(exc).__name__}: {exc}"
            accepted = capture.record(
                direction=direction,
                direction_sequence=direction_sequence,
                kind="data",
                captured_at_utc=captured_at,
                captured_monotonic_ns=captured_monotonic,
                offset=offset,
                payload=payload,
                forwarded=forwarded,
                forwarded_at_utc=utc_now_text(),
                forward_error=forward_error,
                observed_sequence=observed_sequence,
            )
            if not forwarded:
                raise RelayError(forward_error or "forwarding failed")
            if not accepted and capture.policy is StorageFailurePolicy.ABORT:
                raise CaptureAbort(capture.failure_message)

    @staticmethod
    def _abort_writer(writer: asyncio.StreamWriter) -> None:
        transport = writer.transport
        if transport is not None:
            transport.abort()

    @staticmethod
    def _log(level: int, event: str, device: DeviceConfig, **values: Any) -> None:
        LOGGER.log(
            level,
            "%s",
            event,
            extra={"event": event, "device": device.id, **values},
        )


def _endpoint(value: object) -> tuple[str, int] | None:
    if not isinstance(value, tuple) or len(value) < 2:
        return None
    try:
        return str(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None


def _client_allowed(device: DeviceConfig, host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if not isinstance(address, IPv4Address):
        return False
    return address in device.allowed_clients or any(
        address in network for network in device.allowed_networks
    )


async def _forward_eof(writer: asyncio.StreamWriter, timeout: float) -> None:
    if writer.is_closing():
        raise RuntimeError("destination is already closing")
    if writer.can_write_eof():
        writer.write_eof()
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        return
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None:
        raise RuntimeError("destination transport cannot half-close")
    transport_socket.shutdown(socket.SHUT_WR)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    if not writer.is_closing():
        writer.close()
    with contextlib.suppress(ConnectionError, OSError, RuntimeError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=2)


async def _cancel_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _first_task_error(tasks: set[asyncio.Task[Any]]) -> str | None:
    for task in tasks:
        if task.cancelled():
            return "relay pump was cancelled"
        exception = task.exception()
        if exception is not None:
            return f"{type(exception).__name__}: {exception}"
    return None


__all__ = ["RelayError", "RelayService"]
