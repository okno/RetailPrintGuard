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
    assert settings.devices[0].historical_targets == ()


def test_historical_target_whitelist_does_not_change_live_endpoints(tmp_path: Path) -> None:
    data = _base(tmp_path)
    data["devices"][0]["historical_targets"] = [{"ip": "198.51.100.20", "port": 9100}]
    device = load_settings(_write(tmp_path, data)).devices[0]
    assert str(device.target_ip) == "192.0.2.20"
    assert str(device.historical_targets[0].ip) == "198.51.100.20"
    assert device.historical_targets[0].port == 9100
    assert device.additional_ports == ()


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        ([{"ip": "not-an-ip", "port": 9100}], "IPv4 address"),
        ([{"ip": "198.51.100.20", "port": 0}], "greater than or equal"),
        ([{"ip": "198.51.100.20", "port": "9100"}], "valid integer"),
        ([{"ip": "198.51.100.20", "port": True}], "valid integer"),
        ([{"ip": "198.51.100.20", "port": 9100}] * 2, "must be unique"),
        (
            [{"ip": "198.51.100.20", "port": 9100 + index} for index in range(33)],
            "at most 32",
        ),
        ([{"ip": "192.0.2.21", "port": 23}], "must map to one enabled device"),
    ],
)
def test_historical_target_whitelist_rejects_invalid_or_ambiguous_entries(
    tmp_path: Path, targets: list[dict[str, object]], message: str
) -> None:
    data = _base(tmp_path)
    data["devices"][0]["historical_targets"] = targets
    with pytest.raises(ValidationError, match=message):
        load_settings(_write(tmp_path, data))


def test_two_devices_cannot_claim_the_same_historical_target(tmp_path: Path) -> None:
    data = _base(tmp_path)
    for device in data["devices"]:
        device["historical_targets"] = [{"ip": "198.51.100.20", "port": 9100}]
    with pytest.raises(ValidationError, match="must map to one enabled device"):
        load_settings(_write(tmp_path, data))


def test_epson_fiscal_requires_opaque_parser(tmp_path: Path) -> None:
    data = _base(tmp_path)
    data["devices"][1].update({"type": "epson", "parser": "opaque_raw"})
    settings = load_settings(_write(tmp_path, data))
    assert settings.devices[1].type.value == "epson"
    assert settings.devices[1].parser.value == "opaque_raw"
    for parser in ("rch_observed", "escpos"):
        data["devices"][1]["parser"] = parser
        with pytest.raises(ValidationError, match="Epson fiscal devices require"):
            load_settings(_write(tmp_path, data))


def test_epson_additional_ports_are_optional_bounded_and_symmetric(tmp_path: Path) -> None:
    data = _base(tmp_path)
    assert load_settings(_write(tmp_path, data)).devices[0].additional_ports == ()
    data["devices"][1].update(
        {"type": "epson", "parser": "opaque_raw", "additional_ports": list(range(80, 88))}
    )
    device = load_settings(_write(tmp_path, data)).devices[1]
    assert device.additional_ports == tuple(range(80, 88))
    assert device.id == "rch_1"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"type": "pos", "parser": "escpos", "additional_ports": [80]}, "only for Epson"),
        (
            {"type": "rch", "parser": "rch_observed", "additional_ports": [80]},
            "only for Epson",
        ),
        ({"additional_ports": [80, 80]}, "must be unique"),
        ({"additional_ports": [23]}, "differ from both primary ports"),
        ({"listen_port": 9100, "additional_ports": [9100]}, "differ from both primary ports"),
        ({"additional_ports": [0]}, "greater than or equal"),
        ({"additional_ports": [65536]}, "less than or equal"),
        ({"additional_ports": [True]}, "valid integer"),
        ({"additional_ports": ["80"]}, "valid integer"),
        ({"additional_ports": [80.0]}, "valid integer"),
        ({"additional_ports": list(range(80, 89))}, "at most 8"),
        (
            {"listen_ip": "192.0.2.21", "listen_port": 9100, "additional_ports": [80]},
            "listen and target endpoint must differ",
        ),
    ],
)
def test_epson_rejects_invalid_additional_ports(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    data = _base(tmp_path)
    data["devices"][1].update({"type": "epson", "parser": "opaque_raw", **changes})
    with pytest.raises(ValidationError, match=message):
        load_settings(_write(tmp_path, data))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"listen_ip": "192.0.2.11", "listen_port": 80}, "listener endpoints must be unique"),
        ({"target_ip": "192.0.2.21", "target_port": 80}, "target endpoints must be unique"),
        (
            {"target_ip": "192.0.2.11", "target_port": 80},
            "target endpoint cannot also be an enabled listener",
        ),
        (
            {"listen_ip": "192.0.2.21", "listen_port": 80},
            "target endpoint cannot also be an enabled listener",
        ),
    ],
)
def test_additional_ports_participate_in_endpoint_collision_and_loop_checks(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    data = _base(tmp_path)
    data["devices"][1].update(
        {"type": "epson", "parser": "opaque_raw", "additional_ports": [80]}
    )
    data["devices"][0].update(changes)
    with pytest.raises(ValidationError, match=message):
        load_settings(_write(tmp_path, data))
    data["devices"][0]["enabled"] = False
    assert load_settings(_write(tmp_path, data)).devices[1].additional_ports == (80,)


def test_proxy_cli_can_validate_epson_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from retailprintguard.proxy.main import cli

    data = _base(tmp_path)
    data["devices"][1].update({"type": "epson", "parser": "opaque_raw"})
    config = _write(tmp_path, data)
    monkeypatch.setattr(
        "sys.argv", ["proxy", "--config", str(config), "--device-type", "epson", "--check-config"]
    )
    assert cli() == 0
    assert "1 enabled device(s)" in capsys.readouterr().out


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
