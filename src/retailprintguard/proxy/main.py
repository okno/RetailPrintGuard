"""Command-line entrypoint for the protocol-neutral proxy service."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from pathlib import Path

from retailprintguard.common.config import Settings, load_settings
from retailprintguard.common.logging import configure_structured_logging
from retailprintguard.proxy.relay import RelayService


def _select_devices(settings: Settings, device_type: str | None) -> Settings:
    if device_type is not None:
        selected = tuple(
            device for device in settings.devices if device.type.value == device_type
        )
        if not selected:
            raise RuntimeError(f"no {device_type} devices are configured")
        settings = settings.model_copy(update={"devices": selected})
    return settings


async def _run(config_path: Path, device_type: str | None) -> None:
    settings = _select_devices(load_settings(config_path), device_type)
    service = RelayService(settings)
    await service.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stopped.set)
    try:
        await stopped.wait()
    finally:
        await service.stop()


def cli() -> int:
    parser = argparse.ArgumentParser(description="RetailPrintGuard transparent proxy")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--device-type", choices=("pos", "rch"))
    parser.add_argument("--check-config", action="store_true")
    arguments = parser.parse_args()
    settings = _select_devices(load_settings(arguments.config), arguments.device_type)
    if arguments.check_config:
        enabled = sum(device.enabled for device in settings.devices)
        print(f"configuration valid: {enabled} enabled device(s)")
        return 0
    runtime = configure_structured_logging("proxy")
    try:
        asyncio.run(_run(arguments.config, arguments.device_type))
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
