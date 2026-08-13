#!/usr/bin/env python3
"""Validate production-only topology constraints without changing networking."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from retailprintguard.common.config import load_settings

DOCUMENTATION_NETWORKS = (
    IPv4Network("192.0.2.0/24"),
    IPv4Network("198.51.100.0/24"),
    IPv4Network("203.0.113.0/24"),
)


def _production_address(address: IPv4Address, label: str) -> None:
    if any(address in network for network in DOCUMENTATION_NETWORKS):
        raise ValueError(f"{label} uses an RFC 5737 documentation address: {address}")
    if not address.is_private:
        raise ValueError(f"{label} must be an approved private-site IPv4 address: {address}")
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError(f"{label} is not a usable private-site IPv4 address: {address}")


def _assigned_ipv4() -> set[IPv4Address]:
    executable = shutil.which("ip")
    if executable is None:
        raise ValueError("cannot inspect assigned IPv4 addresses: iproute2 is missing")
    try:
        completed = subprocess.run(  # noqa: S603
            [executable, "-j", "-4", "address", "show"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        interfaces = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError(f"cannot inspect assigned IPv4 addresses: {exc}") from exc
    result: set[IPv4Address] = set()
    for interface in interfaces:
        for address in interface.get("addr_info", []):
            if address.get("family") == "inet" and isinstance(address.get("local"), str):
                result.add(IPv4Address(address["local"]))
    return result


def validate(
    path: Path,
    *,
    require_assigned_listeners: bool,
    require_deployment_layout: bool = False,
    quiet: bool = False,
) -> int:
    settings = load_settings(path)
    if require_deployment_layout:
        required_paths = {
            "spool_root": Path("/var/lib/retailprintguard/spool"),
            "archive_root": Path("/var/lib/retailprintguard/archive"),
            "log_root": Path("/var/log/retailprintguard"),
        }
        for name, required in required_paths.items():
            if getattr(settings, name) != required:
                raise ValueError(f"{name} must be {required} for the packaged systemd units")
    enabled = [device for device in settings.devices if device.enabled]
    for device in enabled:
        _production_address(device.listen_ip, f"devices[{device.id}].listen_ip")
        _production_address(device.target_ip, f"devices[{device.id}].target_ip")
    if require_assigned_listeners:
        assigned = _assigned_ipv4()
        missing = sorted(
            str(device.listen_ip) for device in enabled if device.listen_ip not in assigned
        )
        if missing:
            raise ValueError(
                "listener address(es) are not assigned locally; configure them with the host "
                f"network manager before installation: {', '.join(missing)}"
            )
    if not quiet:
        print(f"production configuration valid: {len(enabled)} enabled device(s)")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--require-assigned-listeners", action="store_true")
    parser.add_argument("--require-deployment-layout", action="store_true")
    parser.add_argument(
        "--list-device-directories",
        action="store_true",
        help=(
            "after validation, print one trusted tab-separated "
            "'<type><TAB><id>' line per enabled device"
        ),
    )
    arguments = parser.parse_args()
    try:
        result = validate(
            arguments.config,
            require_assigned_listeners=arguments.require_assigned_listeners,
            require_deployment_layout=arguments.require_deployment_layout,
            quiet=arguments.list_device_directories,
        )
        if arguments.list_device_directories:
            settings = load_settings(arguments.config)
            for device in settings.devices:
                if device.enabled:
                    print(f"{device.type.value}\t{device.id}")
        return result
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
