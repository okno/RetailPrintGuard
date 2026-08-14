from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from retailprintguard.common.config import Settings
from retailprintguard.proxy import spool as spool_module
from retailprintguard.proxy.relay import (
    RelayError,
    RelayService,
    _ActivityClock,
    _RelayCounters,
)
from retailprintguard.proxy.spool import (
    CaptureError,
    CaptureSession,
    Direction,
    PublishedCapture,
    SessionCloseSummary,
    SessionDescriptor,
    StorageFailurePolicy,
)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class FakeDevice:
    def __init__(
        self,
        *,
        banner: bytes = b"",
        response: Callable[[bytes], bytes] = lambda payload: b"ACK:" + payload,
    ) -> None:
        self.banner = banner
        self.response = response
        self.received: list[bytes] = []
        self.server: asyncio.AbstractServer | None = None
        self.connections = 0

    @property
    def port(self) -> int:
        assert self.server is not None
        sockets = self.server.sockets or ()
        assert sockets
        return int(sockets[0].getsockname()[1])

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            if self.banner:
                writer.write(self.banner)
                await writer.drain()
            blocks: list[bytes] = []
            while block := await reader.read(8192):
                blocks.append(block)
            payload = b"".join(blocks)
            self.received.append(payload)
            writer.write(self.response(payload))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


class HoldingDevice(FakeDevice):
    def __init__(self) -> None:
        super().__init__(response=lambda payload: b"FIRST:" + payload)
        self.connected = asyncio.Event()
        self.release = asyncio.Event()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self.connected.set()
        await self.release.wait()
        try:
            blocks: list[bytes] = []
            while block := await reader.read(8192):
                blocks.append(block)
            payload = b"".join(blocks)
            self.received.append(payload)
            writer.write(self.response(payload))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


class StreamingResponseDevice(FakeDevice):
    def __init__(self, chunks: tuple[bytes, ...], delay: float) -> None:
        super().__init__()
        self.chunks = chunks
        self.delay = delay

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            payload = await reader.read()
            self.received.append(payload)
            for chunk in self.chunks:
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(self.delay)
        finally:
            writer.close()
            await writer.wait_closed()


def _settings(
    tmp_path: Path,
    routes: list[dict[str, Any]],
    *,
    storage_failure_policy: str = "continue",
    response_tail_timeout_seconds: float = 1,
) -> Settings:
    root = _posix_config_path(tmp_path)
    return Settings.model_validate(
        {
            "version": 1,
            "timezone": "Europe/Rome",
            "spool_root": f"{root}/spool",
            "archive_root": f"{root}/archive",
            "log_root": f"{root}/log",
            "database_url_env": "RPG_TEST_DATABASE_URL",
            "proxy": {
                "connect_timeout_seconds": 1,
                "forward_timeout_seconds": 1,
                "response_tail_timeout_seconds": response_tail_timeout_seconds,
                "shutdown_grace_seconds": 2,
                "read_chunk_bytes": 512,
                "capture_queue_max_events": 128,
                "max_connections": 32,
                "fsync_each_event": False,
                "storage_failure_policy": storage_failure_policy,
            },
            "devices": routes,
        }
    )


def _posix_config_path(path: Path) -> str:
    rendered = path.as_posix()
    if len(rendered) >= 3 and rendered[1:3] == ":/":
        return rendered[2:]
    return rendered


def _route(
    device_id: str,
    target_port: int,
    *,
    device_type: str = "pos",
) -> dict[str, Any]:
    return {
        "id": device_id,
        "name": f"Synthetic {device_id}",
        "type": device_type,
        "listen_ip": "127.0.0.1",
        "listen_port": _unused_port(),
        "target_ip": "127.0.0.1",
        "target_port": target_port,
        "parser": "rch_observed" if device_type == "rch" else "escpos",
        "bidirectional": True,
        "enabled": True,
        "allowed_clients": ["127.0.0.1"],
    }


async def _half_close(writer: asyncio.StreamWriter) -> None:
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()
        return
    transport_socket = writer.get_extra_info("socket")
    assert transport_socket is not None
    transport_socket.shutdown(socket.SHUT_WR)


async def _exchange(
    endpoint: tuple[str, int], fragments: list[bytes], *, delay: float = 0
) -> bytes:
    reader, writer = await asyncio.open_connection(*endpoint)
    try:
        for fragment in fragments:
            writer.write(fragment)
            await writer.drain()
            if delay:
                await asyncio.sleep(delay)
        await _half_close(writer)
        return await asyncio.wait_for(reader.read(), timeout=3)
    finally:
        writer.close()
        await writer.wait_closed()


async def _wait_for_jobs(service: RelayService, count: int) -> None:
    async with asyncio.timeout(5):
        while len(service.published_jobs) < count:
            await asyncio.sleep(0.01)


def _manifest(job_path: Path) -> dict[str, Any]:
    return json.loads((job_path / "manifest.json").read_text(encoding="utf-8"))


class FailingCaptureManager:
    async def start(self) -> list[Path]:
        return []

    async def stop(self) -> None:
        return None

    async def open_session(
        self, descriptor: SessionDescriptor, policy: StorageFailurePolicy
    ) -> CaptureSession:
        del descriptor, policy
        raise CaptureError("synthetic storage outage")

    def open_session_nowait(
        self, descriptor: SessionDescriptor, policy: StorageFailurePolicy
    ) -> CaptureSession:
        del descriptor, policy
        raise CaptureError("synthetic storage outage")

    def disabled_session(
        self,
        descriptor: SessionDescriptor,
        policy: StorageFailurePolicy,
        error: str,
    ) -> CaptureSession:
        return CaptureSession(None, descriptor, policy, initial_error=error)


@pytest.mark.asyncio
async def test_database_offline_does_not_block_forwarding_or_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RPG_TEST_DATABASE_URL", raising=False)
    device = FakeDevice(response=lambda payload: b"\x06" + payload[-3:])
    await device.start()
    settings = _settings(tmp_path, [_route("pos_offline", device.port)])
    service = RelayService(settings)
    await service.start()
    payload = b"offline-db-print\x1dV\x00"
    try:
        response = await _exchange(service.listener_endpoints["pos_offline"], [payload])
        await _wait_for_jobs(service, 1)
    finally:
        await service.stop()
        await device.stop()

    assert response == b"\x06\x1dV\x00"
    assert device.received == [payload]
    job = service.published_jobs[0].job_path
    assert (job / "client.raw").read_bytes() == payload
    assert (job / "device.raw").read_bytes() == response
    assert (job / ".ready").is_file()
    assert _manifest(job)["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_fragmented_and_malformed_stream_is_forwarded_byte_exact(tmp_path: Path) -> None:
    device = FakeDevice(response=lambda _payload: b"\xff\x00RCH?\x1b")
    await device.start()
    settings = _settings(tmp_path, [_route("pos_binary", device.port)])
    service = RelayService(settings)
    await service.start()
    fragments = [
        b"\x1b@FIRST\x00",
        b"\xff\xfe\x1d(k\x02",
        b"SECOND-DOCUMENT-IN-SAME-STREAM",
        b"\x00\x00\x1dV",
    ]
    payload = b"".join(fragments)
    try:
        response = await _exchange(
            service.listener_endpoints["pos_binary"], fragments, delay=0.01
        )
        await _wait_for_jobs(service, 1)
    finally:
        await service.stop()
        await device.stop()

    assert device.received == [payload]
    assert response == b"\xff\x00RCH?\x1b"
    job = service.published_jobs[0].job_path
    assert (job / "client.raw").read_bytes() == payload
    assert (job / "device.raw").read_bytes() == response
    timeline = [
        json.loads(line)
        for line in (job / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    client_entries = [
        entry
        for entry in timeline
        if entry["direction"] == "client_to_device" and entry["kind"] == "data"
    ]
    assert [entry["offset"] for entry in client_entries] == [
        sum(entry["length"] for entry in client_entries[:index])
        for index in range(len(client_entries))
    ]
    assert sum(entry["length"] for entry in client_entries) == len(payload)


@pytest.mark.asyncio
async def test_fragmentation_fuzz_preserves_payload_and_digest_for_every_partition(
    tmp_path: Path,
) -> None:
    payload = (
        b"\x00\xff\xfe\x1b@"
        b"<document><line quantity='2'>SYNTHETIC</line></document>"
        b"\x1d(k\x04\x00\x00\x01\x02\x03\x1dV\x00"
    ) * 17
    expected_response = b"\x06RESPONSE\x00\xff" + payload[::-1]
    device = FakeDevice(response=lambda _payload: expected_response)
    await device.start()
    service = RelayService(_settings(tmp_path, [_route("pos_fuzz", device.port)]))
    await service.start()

    fragment_widths = hashlib.sha256(b"retailprintguard-fragmentation-fuzz-v1").digest()
    random_fragments: list[bytes] = []
    cursor = 0
    fragment_index = 0
    while cursor < len(payload):
        width = fragment_widths[fragment_index % len(fragment_widths)] % 37 + 1
        random_fragments.append(payload[cursor : cursor + width])
        cursor += width
        fragment_index += 1
    partitions: list[tuple[str, list[bytes], float]] = [
        ("single", [payload], 0),
        ("bytewise", [payload[index : index + 1] for index in range(len(payload))], 0),
        ("fixed", [payload[index : index + 13] for index in range(0, len(payload), 13)], 0),
        ("random", random_fragments, 0),
        ("random-delayed", random_fragments, 0.0001),
    ]

    try:
        for index, (_name, fragments, delay) in enumerate(partitions, start=1):
            response = await _exchange(
                service.listener_endpoints["pos_fuzz"], fragments, delay=delay
            )
            assert response == expected_response
            await _wait_for_jobs(service, index)
    finally:
        await service.stop()
        await device.stop()

    assert device.received == [payload] * len(partitions)
    assert len(service.published_jobs) == len(partitions)
    expected_request_sha = hashlib.sha256(payload).hexdigest()
    expected_response_sha = hashlib.sha256(expected_response).hexdigest()
    for published in service.published_jobs:
        captured_request = (published.job_path / "client.raw").read_bytes()
        captured_response = (published.job_path / "device.raw").read_bytes()
        assert len(captured_request) == len(payload)
        assert len(captured_response) == len(expected_response)
        assert hashlib.sha256(captured_request).hexdigest() == expected_request_sha
        assert hashlib.sha256(captured_response).hexdigest() == expected_response_sha
        assert _manifest(published.job_path)["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_four_devices_concurrently_remain_isolated_and_rch_is_full_duplex(
    tmp_path: Path,
) -> None:
    devices = [
        FakeDevice(response=lambda payload, marker=marker: marker + payload)
        for marker in (b"P1:", b"P2:", b"P3:")
    ]
    rch = FakeDevice(banner=b"RCH-READY|", response=lambda payload: b"RCH-ACK:" + payload)
    all_devices = [*devices, rch]
    for fake in all_devices:
        await fake.start()
    routes = [
        _route("pos_one", devices[0].port),
        _route("pos_two", devices[1].port),
        _route("pos_three", devices[2].port),
        _route("rch_one", rch.port, device_type="rch"),
    ]
    settings = _settings(tmp_path, routes)
    service = RelayService(settings)
    await service.start()
    payloads = {
        "pos_one": b"kitchen-one\x00",
        "pos_two": b"bar-two\xff",
        "pos_three": b"secondary-three\x1dV",
        "rch_one": b"rch-request\x00\x1b",
    }
    try:
        responses = await asyncio.gather(
            *(
                _exchange(service.listener_endpoints[device_id], [payload])
                for device_id, payload in payloads.items()
            )
        )
        await _wait_for_jobs(service, 4)
    finally:
        await service.stop()
        for fake in all_devices:
            await fake.stop()

    assert responses == [
        b"P1:" + payloads["pos_one"],
        b"P2:" + payloads["pos_two"],
        b"P3:" + payloads["pos_three"],
        b"RCH-READY|RCH-ACK:" + payloads["rch_one"],
    ]
    assert [fake.received for fake in all_devices] == [
        [payloads["pos_one"]],
        [payloads["pos_two"]],
        [payloads["pos_three"]],
        [payloads["rch_one"]],
    ]
    jobs_by_device = {
        _manifest(published.job_path)["session"]["device_id"]: published.job_path
        for published in service.published_jobs
    }
    assert set(jobs_by_device) == set(payloads)
    for device_id, payload in payloads.items():
        assert (jobs_by_device[device_id] / "client.raw").read_bytes() == payload
    assert (jobs_by_device["rch_one"] / "device.raw").read_bytes() == responses[3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "forwarded"),
    [("continue", True), ("abort", False)],
)
async def test_storage_failure_policy_is_explicit(
    tmp_path: Path, policy: str, forwarded: bool
) -> None:
    device = FakeDevice(response=lambda payload: b"OK" if payload else b"")
    await device.start()
    settings = _settings(
        tmp_path,
        [_route("pos_storage", device.port)],
        storage_failure_policy=policy,
    )
    capture_manager = FailingCaptureManager()
    service = RelayService(settings, capture_manager=capture_manager)  # type: ignore[arg-type]
    await service.start()
    reader, writer = await asyncio.open_connection(*service.listener_endpoints["pos_storage"])
    try:
        writer.write(b"must-not-be-mutated")
        await writer.drain()
        with contextlib.suppress(ConnectionError, OSError):
            await _half_close(writer)
        with contextlib.suppress(ConnectionError, OSError):
            await asyncio.wait_for(reader.read(), timeout=2)
        async with asyncio.timeout(2):
            while not device.received:
                await asyncio.sleep(0.01)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        await service.stop()
        await device.stop()

    assert device.received == ([b"must-not-be-mutated"] if forwarded else [b""])
    assert service.published_jobs == []


@pytest.mark.asyncio
async def test_acl_rejection_never_connects_to_physical_device(tmp_path: Path) -> None:
    device = FakeDevice()
    await device.start()
    route = _route("pos_acl", device.port)
    route["allowed_clients"] = ["192.0.2.99"]
    settings = _settings(tmp_path, [route])
    service = RelayService(settings)
    await service.start()
    reader, writer = await asyncio.open_connection(*service.listener_endpoints["pos_acl"])
    try:
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(b"rejected")
            await writer.drain()
        with contextlib.suppress(ConnectionError, OSError):
            await asyncio.wait_for(reader.read(), timeout=1)
        await asyncio.sleep(0.05)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        await service.stop()
        await device.stop()

    assert device.connections == 0
    assert service.published_jobs == []


@pytest.mark.asyncio
async def test_capture_start_failure_continues_only_when_configured(tmp_path: Path) -> None:
    device = FakeDevice(response=lambda _payload: b"FORWARDED")
    await device.start()
    route = _route("pos_start_storage", device.port)

    continue_settings = _settings(
        tmp_path,
        [route],
        storage_failure_policy="continue",
    )
    continue_settings.spool_root.parent.mkdir(parents=True, exist_ok=True)
    continue_settings.spool_root.write_bytes(b"not-a-directory")
    service = RelayService(continue_settings)
    await service.start()
    try:
        response = await _exchange(
            service.listener_endpoints["pos_start_storage"], [b"still-print"]
        )
    finally:
        await service.stop()
    assert response == b"FORWARDED"
    assert device.received == [b"still-print"]
    assert service.published_jobs == []

    abort_root = tmp_path / "abort-case"
    abort_settings = _settings(
        abort_root,
        [_route("pos_abort_storage", device.port)],
        storage_failure_policy="abort",
    )
    abort_settings.spool_root.parent.mkdir(parents=True, exist_ok=True)
    abort_settings.spool_root.write_bytes(b"not-a-directory")
    abort_service = RelayService(abort_settings)
    with pytest.raises(OSError):
        await abort_service.start()
    assert device.received == [b"still-print"]
    await device.stop()


@pytest.mark.asyncio
async def test_physical_target_lock_rejects_a_concurrent_client(tmp_path: Path) -> None:
    device = HoldingDevice()
    await device.start()
    settings = _settings(tmp_path, [_route("pos_exclusive", device.port)])
    service = RelayService(settings)
    await service.start()
    first_reader, first_writer = await asyncio.open_connection(
        *service.listener_endpoints["pos_exclusive"]
    )
    await asyncio.wait_for(device.connected.wait(), timeout=2)

    second_reader, second_writer = await asyncio.open_connection(
        *service.listener_endpoints["pos_exclusive"]
    )
    try:
        with contextlib.suppress(ConnectionError, OSError):
            second_writer.write(b"must-not-queue")
            await second_writer.drain()
        with contextlib.suppress(ConnectionError, OSError):
            await asyncio.wait_for(second_reader.read(), timeout=1)
        await asyncio.sleep(0.05)
        assert device.connections == 1

        first_writer.write(b"first-session")
        await first_writer.drain()
        await _half_close(first_writer)
        device.release.set()
        response = await asyncio.wait_for(first_reader.read(), timeout=2)
        await _wait_for_jobs(service, 1)
    finally:
        for writer in (first_writer, second_writer):
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
        device.release.set()
        await service.stop()
        await device.stop()

    assert response == b"FIRST:first-session"
    assert device.received == [b"first-session"]
    assert len(service.published_jobs) == 1


@pytest.mark.asyncio
async def test_active_reverse_tail_timeout_resets_on_each_chunk(tmp_path: Path) -> None:
    chunks = tuple(bytes([value]) for value in range(12))
    device = StreamingResponseDevice(chunks, delay=0.02)
    await device.start()
    settings = _settings(
        tmp_path,
        [_route("rch_streaming_tail", device.port, device_type="rch")],
        # Keep the idle window shorter than the complete 240 ms response but
        # large enough for coarse Windows CI scheduling under parallel load.
        response_tail_timeout_seconds=0.12,
    )
    service = RelayService(settings)
    await service.start()
    try:
        response = await _exchange(
            service.listener_endpoints["rch_streaming_tail"], [b"long-response-request"]
        )
        await _wait_for_jobs(service, 1)
    finally:
        await service.stop()
        await device.stop()

    assert response == b"".join(chunks)
    assert device.received == [b"long-response-request"]
    manifest = _manifest(service.published_jobs[0].job_path)
    assert manifest["status"] == "COMPLETE"
    assert manifest["close_reason"] == "clean_bidirectional_eof"


@pytest.mark.asyncio
async def test_forward_error_aborts_buffered_transport_immediately(tmp_path: Path) -> None:
    class OneChunkReader:
        async def read(self, _maximum: int) -> bytes:
            return b"payload-already-queued"

    class TrackingTransport:
        def __init__(self) -> None:
            self.aborted = False

        def abort(self) -> None:
            self.aborted = True

    class TimedOutWriter:
        def __init__(self) -> None:
            self.transport = TrackingTransport()
            self.written = b""

        def write(self, payload: bytes) -> None:
            self.written += payload

        async def drain(self) -> None:
            raise TimeoutError("synthetic backpressure timeout")

    class RecordingCapture:
        policy = StorageFailurePolicy.CONTINUE
        failure_message = ""

        def record(self, **_values: Any) -> bool:
            return True

    settings = _settings(tmp_path, [_route("pos_abort_buffer", 9101)])
    service = RelayService(settings)
    writer = TimedOutWriter()
    with pytest.raises(RelayError, match="synthetic backpressure timeout"):
        await service._pump(  # type: ignore[arg-type]
            OneChunkReader(),
            writer,
            Direction.CLIENT_TO_DEVICE,
            RecordingCapture(),  # type: ignore[arg-type]
            _RelayCounters(),
            _ActivityClock(),
        )

    assert writer.written == b"payload-already-queued"
    assert writer.transport.aborted is True


@pytest.mark.asyncio
async def test_slow_capture_open_never_delays_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initializer_started = threading.Event()
    release_initializer = threading.Event()
    original_initializer = spool_module._SessionSpool.__init__

    def delayed_initializer(*args: Any, **kwargs: Any) -> None:
        initializer_started.set()
        if not release_initializer.wait(timeout=5):
            raise TimeoutError("test did not release capture initializer")
        original_initializer(*args, **kwargs)

    monkeypatch.setattr(spool_module._SessionSpool, "__init__", delayed_initializer)
    device = FakeDevice(response=lambda _payload: b"FORWARDED-WHILE-DISK-SLOW")
    await device.start()
    settings = _settings(tmp_path, [_route("pos_slow_capture_open", device.port)])
    service = RelayService(settings)
    await service.start()
    exchange = asyncio.create_task(
        _exchange(service.listener_endpoints["pos_slow_capture_open"], [b"print-now"])
    )
    try:
        assert await asyncio.to_thread(initializer_started.wait, 1)
        response = await asyncio.wait_for(exchange, timeout=1)
        assert response == b"FORWARDED-WHILE-DISK-SLOW"
        assert device.received == [b"print-now"]
    finally:
        release_initializer.set()
        with contextlib.suppress(Exception):
            await exchange
        await service.stop()
        await device.stop()

    await _wait_for_jobs(service, 1)
    manifest = _manifest(service.published_jobs[0].job_path)
    assert manifest["status"] == "COMPLETE"
    assert manifest["storage_complete"] is True


@pytest.mark.asyncio
async def test_published_job_history_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("retailprintguard.proxy.relay._PUBLISHED_JOB_HISTORY_LIMIT", 2)
    settings = _settings(tmp_path, [_route("pos_history", 9101)])
    service = RelayService(settings)
    device = settings.devices[0]
    summary = SessionCloseSummary(
        closed_at_utc="2026-08-14T00:00:01Z",
        closed_monotonic_ns=2,
        close_reason="test",
        transport_complete=True,
        observed_bytes={direction.value: 0 for direction in Direction},
        observed_chunks={direction.value: 0 for direction in Direction},
    )

    class PublishedCaptureResult:
        def __init__(self, index: int) -> None:
            self.index = index

        async def finalize(self, _summary: SessionCloseSummary) -> PublishedCapture:
            path = tmp_path / str(self.index)
            return PublishedCapture(path, path / "manifest.json", path / ".ready", "0" * 64)

    for index in range(4):
        descriptor = SessionDescriptor(
            session_id=f"session-{index}",
            job_id=f"job-{index}",
            device_id=device.id,
            device_name=device.name,
            device_type=device.type.value,
            parser=device.parser.value,
            client_endpoint=("127.0.0.1", 50000 + index),
            listener_endpoint=(str(device.listen_ip), device.listen_port),
            target_endpoint=(str(device.target_ip), device.target_port),
            connected_at_utc="2026-08-14T00:00:00Z",
            connected_monotonic_ns=1,
        )
        await service._finalize_capture(  # type: ignore[arg-type]
            device, descriptor, PublishedCaptureResult(index), summary
        )

    assert [item.job_path.name for item in service.published_jobs] == ["2", "3"]
