from __future__ import annotations

from ipaddress import IPv4Network
from pathlib import Path

import pytest

from retailprintguard.common.config import Settings
from retailprintguard.proxy.legacy_config import (
    LegacyConfigError,
    compile_legacy_configs,
    write_legacy_configs,
)


def _settings(tmp_path: Path) -> Settings:
    devices = []
    for index in range(3):
        devices.append(
            {
                "id": f"pos_{index + 1}",
                "name": f"POS {index + 1}",
                "type": "pos",
                "listen_ip": f"192.0.2.{220 + index}",
                "listen_port": 9100,
                "target_ip": f"192.0.2.{200 + index}",
                "target_port": 9100,
                "parser": "escpos",
                "allowed_networks": ["192.0.2.0/24"],
            }
        )
    devices.append(
        {
            "id": "rch_1",
            "name": "RCH",
            "type": "rch",
            "listen_ip": "192.0.2.231",
            "listen_port": 23,
            "target_ip": "192.0.2.251",
            "target_port": 23,
            "parser": "rch_observed",
            "allowed_networks": ["192.0.2.0/24"],
        }
    )
    root = tmp_path.as_posix()
    if len(root) >= 3 and root[1:3] == ":/":
        root = root[2:]
    return Settings.model_validate(
        {
            "spool_root": f"{root}/spool",
            "archive_root": f"{root}/archive",
            "log_root": f"{root}/log",
            "database_url_env": "RPG_DATABASE_URL",
            "devices": devices,
        }
    )


def test_compiler_emits_positional_pos_and_single_rch_without_secrets(tmp_path: Path) -> None:
    compiled = compile_legacy_configs(_settings(tmp_path))

    assert set(compiled.files) == {"printproxy.conf", "commercialrchproxy.conf"}
    pos = compiled.files["printproxy.conf"]
    assert "LISTEN_IP=192.0.2.220,192.0.2.221,192.0.2.222\n" in pos
    assert "PRINTER_IP=192.0.2.200,192.0.2.201,192.0.2.202\n" in pos
    assert "DELIVERY_MODE=transparent_duplex\n" in pos
    rch = compiled.files["commercialrchproxy.conf"]
    assert "LISTEN_PORT=23\n" in rch
    assert "STORAGE_FAILURE_POLICY=continue\n" in rch
    combined = pos + rch
    assert "RPG_DATABASE_URL" not in combined
    assert "DATABASE" not in combined
    assert "password" not in combined.lower()
    assert "token" not in combined.lower()


def test_compiler_writes_atomically_and_refuses_implicit_overwrite(tmp_path: Path) -> None:
    compiled = compile_legacy_configs(_settings(tmp_path))
    destination = tmp_path / "legacy"
    written = write_legacy_configs(compiled, destination)
    assert {path.name for path in written} == set(compiled.files)
    assert not list(destination.glob("*.tmp"))
    with pytest.raises(FileExistsError):
        write_legacy_configs(compiled, destination)


def test_compiler_rejects_pos_routes_with_unrepresentable_per_route_acl(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    devices = list(settings.devices)
    devices[1] = devices[1].model_copy(
        update={"allowed_networks": (IPv4Network("198.51.100.0/24"),)}
    )
    incompatible = settings.model_copy(update={"devices": tuple(devices)})
    with pytest.raises(LegacyConfigError, match="one global ACL"):
        compile_legacy_configs(incompatible)
