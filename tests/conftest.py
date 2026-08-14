"""Global test safety guard: test code may open loopback sockets only."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Generator
from typing import Any

import pytest


def _assert_loopback(host: Any) -> None:
    if host is None:
        return
    value = str(host).strip().lower()
    if value == "localhost":
        return
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise RuntimeError(f"test network guard rejected hostname: {value!r}") from exc
    if not address.is_loopback:
        raise RuntimeError(f"test network guard rejected non-loopback address: {address}")


@pytest.fixture(autouse=True)
def loopback_only_network(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Prevent a test regression from reaching any LAN or Internet endpoint."""

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_open_connection = asyncio.open_connection

    def guarded_connect(instance: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address:
            _assert_loopback(address[0])
        return original_connect(instance, address)

    def guarded_connect_ex(instance: socket.socket, address: Any) -> int:
        if isinstance(address, tuple) and address:
            _assert_loopback(address[0])
        return original_connect_ex(instance, address)

    async def guarded_open_connection(
        host: str | None = None,
        port: int | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if kwargs.get("sock") is None:
            _assert_loopback(host)
        return await original_open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(asyncio, "open_connection", guarded_open_connection)
    yield
