from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from retailprintguard.common.config import load_settings

# Subprocess argv values below are trusted executables and test-created paths.
# No shell is involved.

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
SCRIPTS = ROOT / "scripts"

SERVICE_NAMES = {
    "retailprintguard-pos-proxy.service",
    "retailprintguard-rch-proxy.service",
    "retailprintguard-ingestion.service",
    "retailprintguard-parser.service",
    "retailprintguard-correlation.service",
    "retailprintguard-fraud.service",
    "retailprintguard-api.service",
}
BACKUP_SERVICE = "retailprintguard-backup.service"


def test_expected_services_are_separate_and_hardened() -> None:
    assert {path.name for path in SYSTEMD.glob("*.service")} == SERVICE_NAMES | {
        BACKUP_SERVICE
    }
    for name in SERVICE_NAMES:
        content = (SYSTEMD / name).read_text(encoding="utf-8")
        assert "NoNewPrivileges=yes" in content
        assert "ProtectSystem=strict" in content
        assert "PrivateDevices=yes" in content
        assert "RestrictAddressFamilies=AF_UNIX AF_INET" in content
        assert "Restart=on-failure" in content

    backup = (SYSTEMD / BACKUP_SERVICE).read_text(encoding="utf-8")
    assert "Type=oneshot" in backup
    assert "ExecStart=/opt/retailprintguard/current/scripts/backup.sh" in backup
    assert "NoNewPrivileges=yes" in backup
    assert "ProtectSystem=strict" in backup
    assert "SupplementaryGroups=retailprintguard-spool" in backup
    timer = (SYSTEMD / "retailprintguard-backup.timer").read_text(encoding="utf-8")
    assert "Persistent=yes" in timer
    assert "retailprintguard-backup.service" in timer

    pos = (SYSTEMD / "retailprintguard-pos-proxy.service").read_text(encoding="utf-8")
    rch = (SYSTEMD / "retailprintguard-rch-proxy.service").read_text(encoding="utf-8")
    assert "--device-type pos" in pos
    assert "--device-type rch" in rch
    for proxy in (pos, rch):
        assert "EnvironmentFile=/etc/retailprintguard/database.env" not in proxy
        assert "UnsetEnvironment=RPG_DATABASE_URL" in proxy
    assert "User=retailprintguard-pos-proxy" in pos
    assert "User=retailprintguard-rch-proxy" in rch
    ingestion = (SYSTEMD / "retailprintguard-ingestion.service").read_text(encoding="utf-8")
    assert "ReadOnlyPaths=/var/lib/retailprintguard/spool" in ingestion
    assert "ReadWritePaths=/var/lib/retailprintguard/spool" not in ingestion
    assert "--json-logs" in (SCRIPTS / "run_ingestion.sh").read_text(encoding="utf-8")
    for worker in (
        "retailprintguard-parser.service",
        "retailprintguard-correlation.service",
        "retailprintguard-fraud.service",
    ):
        assert "--json-logs" in (SYSTEMD / worker).read_text(encoding="utf-8")
    for control in SERVICE_NAMES - {
        "retailprintguard-pos-proxy.service",
        "retailprintguard-rch-proxy.service",
    }:
        content = (SYSTEMD / control).read_text(encoding="utf-8")
        assert "EnvironmentFile=/etc/retailprintguard/database.env" in content
        assert "CapabilityBoundingSet=\n" in content


def test_nginx_is_loopback_only_by_default() -> None:
    content = (ROOT / "deploy/nginx/retailprintguard.conf").read_text(encoding="utf-8")
    assert "listen 127.0.0.1:8081;" in content
    assert "proxy_pass http://127.0.0.1:8080;" in content
    assert "listen 0.0.0.0" not in content
    assert "add_header Content-Security-Policy" in content


def test_installer_never_mutates_host_networking_and_requires_site_validation() -> None:
    install = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    forbidden = (
        "ip address add",
        "ip addr add",
        "ip route add",
        "iptables",
        "nft add",
        "nmcli connection modify",
    )
    assert all(command not in install for command in forbidden)
    assert "--require-assigned-listeners" in install
    assert "requirements/production.lock" in install
    assert "requirements/build.lock" in install
    assert install.index('-r "${release_path}/requirements/build.lock"') < install.index(
        '-r "${release_path}/requirements/production.lock"'
    )
    assert 'pip\" install --require-hashes' in install
    build_lock = (ROOT / "requirements" / "build.lock").read_text(encoding="utf-8")
    assert "setuptools==80.9.0" in build_lock
    assert "wheel==0.45.1" in build_lock
    assert build_lock.count("--hash=sha256:") == 3
    assert "--require-hashes" in install


def test_virtualenv_is_built_at_final_path_and_entrypoints_are_verified() -> None:
    install = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    assert 'python3 -m venv "${release_path}/.venv"' in install
    assert 'python3 -m venv "${stage}/.venv"' not in install
    assert 'mv -- "${stage}" "${release_path}"' not in install
    assert "installed entrypoint has a non-final shebang" in install
    assert '"${release_path}/.venv/bin/python" -m alembic' in install
    assert 'chmod -R a+rX,go-w "${release_path}"' in install
    assert 'runuser -u "${service_identity}"' in install
    assert "release is not executable by" in install


def test_no_start_stages_without_switching_current_release() -> None:
    install = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    no_start = install.index('if [[ "${start_services}" == no ]]')
    activate_app = install.index('rpg_atomic_symlink "${release_path}" "${RPG_CURRENT_LINK}"')
    activate_web = install.index('rpg_atomic_symlink "${web_release}" "${RPG_WEB_CURRENT}"')
    assert no_start < activate_app < activate_web
    assert '"${RPG_STATE_ROOT}/staged-release"' in install
    assert '"${RPG_STATE_ROOT}/staged-web-release"' in install
    assert "exit 0" in install[no_start:activate_app]


def test_rollback_switches_application_and_its_mapped_frontend() -> None:
    rollback = (SCRIPTS / "rollback.sh").read_text(encoding="utf-8")
    assert 'release-web-${release}' in rollback
    assert 'rpg_atomic_symlink "${target}" "${RPG_CURRENT_LINK}"' in rollback
    assert 'rpg_atomic_symlink "${web_target}" "${RPG_WEB_CURRENT}"' in rollback
    assert "trap restore_links ERR" in rollback


def test_backup_and_restore_fail_closed_for_evidence() -> None:
    backup = (SCRIPTS / "backup.sh").read_text(encoding="utf-8")
    restore = (SCRIPTS / "restore.sh").read_text(encoding="utf-8")
    assert 'find "${RPG_SPOOL_ROOT}" -type f -name .ready -print0 >"${marker_list}"' in backup
    assert "cannot enumerate ready evidence for backup" in backup
    assert 'cmp -s -- "${candidate}" "${existing}"' in restore
    assert "restore collision differs from backup" in restore
    assert '--chown="${copied_owner}"' in restore


def test_restore_reapplies_isolated_proxy_owners() -> None:
    install = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    restore = (SCRIPTS / "restore.sh").read_text(encoding="utf-8")
    assert "retailprintguard-proxy:" not in restore
    assert "--list-device-directories" in restore
    assert "while IFS=$'\\t' read -r device_type device_id; do" in install
    assert "while IFS=$'\\t' read -r device_type device_id; do" in restore
    assert "device_owner=retailprintguard-pos-proxy" in restore
    assert "device_owner=retailprintguard-rch-proxy" in restore


def test_ingestion_example_names_the_canonical_cli_option() -> None:
    content = (ROOT / "deploy/ingestion.env.example").read_text(encoding="utf-8")
    assert "--canonical-root" in content
    assert "--spool-root" not in content


def test_legacy_cleanup_is_dry_run_backup_first_and_non_destructive() -> None:
    cleanup = (SCRIPTS / "cleanup_legacy.sh").read_text(encoding="utf-8")
    assert 'execute=no' in cleanup
    assert 'if [[ "${execute}" != yes ]]' in cleanup
    assert "--network-handover-confirmed" in cleanup
    assert "--network-reapply-helper" in cleanup
    assert "--firewall-handover-confirmed" in cleanup
    assert "active TCP session belongs to" in cleanup
    assert "gzip --test" in cleanup
    assert "tar --list --gzip" in cleanup
    assert "files.jsonl" in cleanup
    assert "Preserved /etc state is evidence" in cleanup
    assert "-e /etc/printproxy/install-state" not in cleanup
    assert "-e /etc/commercialrchproxy ||" not in cleanup
    assert 'removal_started=yes' in cleanup
    assert cleanup.index('systemctl stop printproxy-vip-watch.timer') < cleanup.index(
        '"${rch_network_helper}" uninstall --yes'
    )
    assert cleanup.index('sha256sum --check --strict') < cleanup.index(
        'removal_started=yes'
    )
    assert cleanup.index('Reapplying the approved persistent listener') < cleanup.index(
        'listener ${listener} disappeared'
    )
    assert '"${rch_uninstaller}"' in cleanup
    assert '"${printproxy_uninstaller}"' in cleanup
    assert "--purge-data" not in cleanup
    assert "--purge-config" not in cleanup
    assert "--confirm-purge" not in cleanup
    assert "rm -rf -- /etc/printproxy" not in cleanup
    assert "rm -rf -- /var/lib/printproxy" not in cleanup
    assert "rm -rf -- /etc/commercialrchproxy" not in cleanup
    assert "rm -rf -- /var/lib/commercialrchproxy" not in cleanup


def _write_config(tmp_path: Path, listen_ip: str, target_ip: str) -> Path:
    document = {
        "spool_root": "/var/lib/retailprintguard/spool",
        "archive_root": "/var/lib/retailprintguard/archive",
        "log_root": "/var/log/retailprintguard",
        "devices": [
            {
                "id": "pos_test",
                "name": "POS test",
                "type": "pos",
                "listen_ip": listen_ip,
                "listen_port": 9100,
                "target_ip": target_ip,
                "target_port": 9100,
                "parser": "escpos",
                "allowed_networks": ["10.20.0.0/24"],
            }
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_site_validator_accepts_private_values_and_rejects_rfc5737(tmp_path: Path) -> None:
    valid = _write_config(tmp_path, "10.20.0.10", "10.20.0.20")
    command = [
        sys.executable,
        str(SCRIPTS / "validate_site_config.py"),
        "--config",
        str(valid),
        "--require-deployment-layout",
    ]
    assert subprocess.run(  # noqa: S603
        command, check=False, capture_output=True
    ).returncode == 0
    settings = load_settings(valid)
    assert str(settings.devices[0].listen_ip) == "10.20.0.10"

    documentation = _write_config(tmp_path, "192.0.2.10", "192.0.2.20")
    rejected = subprocess.run(  # noqa: S603
        [*command[:2], "--config", str(documentation)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 2
    assert "RFC 5737" in rejected.stderr


@pytest.mark.parametrize("script", sorted(SCRIPTS.glob("*.sh")))
def test_scripts_are_valid_bash(script: Path) -> None:
    bash = Path("/bin/bash")
    if not bash.exists():
        pytest.skip("POSIX bash is not available on this host")
    completed = subprocess.run(  # noqa: S603
        [str(bash), "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
