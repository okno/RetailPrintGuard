from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from retailprintguard.common.config import load_settings
from scripts.validate_site_config import cli as validate_site_config_cli


def _base(tmp_path: Path) -> dict[str, object]:
    return {
        "version": 1,
        "timezone": "Europe/Rome",
        "spool_root": "/var/lib/retailprintguard-test/spool",
        "archive_root": "/var/lib/retailprintguard-test/archive",
        "log_root": "/var/log/retailprintguard-test",
        "database_url_env": "RPG_DATABASE_URL",
        "devices": [
            {
                "id": "pos_1",
                "name": "POS sintetica",
                "type": "pos",
                "listen_ip": "192.0.2.10",
                "listen_port": 9100,
                "target_ip": "192.0.2.20",
                "target_port": 9100,
                "parser": "escpos",
                "bidirectional": True,
                "enabled": True,
                "allowed_networks": ["192.0.2.0/24"],
            },
            {
                "id": "rch_1",
                "name": "RCH sintetica",
                "type": "rch",
                "listen_ip": "192.0.2.11",
                "listen_port": 23,
                "target_ip": "192.0.2.21",
                "target_port": 23,
                "parser": "rch_observed",
                "bidirectional": True,
                "enabled": True,
                "allowed_networks": ["192.0.2.0/24"],
            },
        ],
    }


def _write(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loads_strict_multi_device_configuration(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _base(tmp_path)))

    assert [device.id for device in settings.devices] == ["pos_1", "rch_1"]
    assert settings.devices[1].bidirectional is True
    assert settings.timezone == "Europe/Rome"


@pytest.mark.parametrize(
    ("configured", "canonical"),
    [
        ("02:ab:cd:00:01:02", "02:AB:CD:00:01:02"),
        ("02-AB-CD-00-01-02", "02:AB:CD:00:01:02"),
        ("02abcd000102", "02:AB:CD:00:01:02"),
    ],
)
def test_device_metadata_normalizes_mac_and_labels(
    tmp_path: Path, configured: str, canonical: str
) -> None:
    data = _base(tmp_path)
    data["devices"][0].update(  # type: ignore[index, union-attr]
        {
            "mac_address": configured,
            "department": "  Reparto sintetico  ",
            "role": "  stampa_comande  ",
        }
    )

    device = load_settings(_write(tmp_path, data)).devices[0]

    assert device.mac_address == canonical
    assert device.department == "Reparto sintetico"
    assert device.role == "stampa_comande"


@pytest.mark.parametrize(
    "invalid",
    [
        "02:AB-CD:00:01:02",
        "02:AB:CD:00:01",
        "not-a-mac-address",
        123,
    ],
)
def test_device_metadata_rejects_noncanonicalizable_mac(
    tmp_path: Path, invalid: object
) -> None:
    data = _base(tmp_path)
    data["devices"][0]["mac_address"] = invalid  # type: ignore[index]

    with pytest.raises(ValidationError, match="MAC address"):
        load_settings(_write(tmp_path, data))


def test_device_directory_listing_uses_shell_safe_tab_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write(tmp_path, _base(tmp_path))
    monkeypatch.setattr("scripts.validate_site_config.validate", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "sys.argv",
        [
            "validate_site_config.py",
            "--config",
            str(config_path),
            "--list-device-directories",
        ],
    )

    assert validate_site_config_cli() == 0
    assert capsys.readouterr().out.splitlines() == ["pos\tpos_1", "rch\trch_1"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update({"unknown": True}), "Extra inputs"),
        (lambda data: data["devices"].append(dict(data["devices"][0])), "device ids"),
        (
            lambda data: data["devices"][0].update({"parser": "rch_observed"}),
            "POS devices require",
        ),
        (lambda data: data.update({"spool_root": "/"}), "unsafe system root"),
        (
            lambda data: data["devices"][1].update(
                {
                    "target_ip": data["devices"][0]["target_ip"],
                    "target_port": data["devices"][0]["target_port"],
                }
            ),
            "target endpoints",
        ),
    ],
)
def test_rejects_incoherent_configuration(tmp_path: Path, mutation: object, message: str) -> None:
    data = _base(tmp_path)
    mutation(data)  # type: ignore[operator]
    with pytest.raises(ValidationError, match=message):
        load_settings(_write(tmp_path, data))


def test_database_secret_is_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(_write(tmp_path, _base(tmp_path)))
    monkeypatch.delenv("RPG_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="RPG_DATABASE_URL"):
        settings.database_url()
    monkeypatch.setenv("RPG_DATABASE_URL", "mysql+pymysql://user:secret@127.0.0.1/db")
    assert settings.database_url().get_secret_value().startswith("mysql+pymysql://")
