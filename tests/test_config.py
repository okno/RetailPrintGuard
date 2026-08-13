from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from retailprintguard.common.config import load_settings


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
