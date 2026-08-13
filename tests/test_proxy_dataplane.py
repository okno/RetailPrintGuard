from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from retailprintguard.common.config import Settings
from retailprintguard.proxy.relay import RelayService
from retailprintguard.proxy.spool import (
    CaptureError,
    CaptureSession,
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


def _settings(
    tmp_path: Path,
    routes: list[dict[str, Any]],
    *,
    storage_failure_policy: str = "continue",
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
                "response_tail_timeout_seconds": 1,
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
