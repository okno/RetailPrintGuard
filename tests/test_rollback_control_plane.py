"""Hermetic execution of the production rollback path; no host services used."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="production rollback requires Linux systemd")
def test_control_plane_rollback_gates_proxy_changes_and_never_restarts_them(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    root = tmp_path / "sandbox"
    bin_dir = root / "bin"
    scripts = root / "scripts"
    releases = root / "releases"
    state = root / "state"
    web = root / "web"
    units = root / "units"
    for directory in (bin_dir, scripts, releases, state, web, units):
        directory.mkdir(parents=True)

    old_id, new_id = "a" * 20, "b" * 20
    old, new = releases / old_id, releases / new_id
    for release in (old, new):
        (release / ".venv/bin").mkdir(parents=True)
        (release / "scripts").mkdir()
        (release / "src/retailprintguard/proxy").mkdir(parents=True)
        (release / "src/retailprintguard/proxy/relay.py").write_text("# same relay\n")
        (release / ".release-complete").touch()
        python = release / ".venv/bin/python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        (release / "scripts/validate_site_config.py").touch()
        for name in ("pos", "rch", "epson"):
            unit = f"retailprintguard-{name}-proxy.service"
            content = f"[Unit]\nDescription=synthetic {name} proxy\n"
            (release / "systemd").mkdir(exist_ok=True)
            (release / "systemd" / unit).write_text(content)
            (units / unit).write_text(content)

    for release in (old, new):
        frontend = web / release.name
        frontend.mkdir()
        (frontend / "index.html").write_text("synthetic web")
        (state / f"release-web-{release.name}").write_text(f"{frontend}\n")
    (root / "current").symlink_to(new, target_is_directory=True)
    (root / "web-current").symlink_to(web / new_id, target_is_directory=True)
    (state / "previous-release").write_text(f"{old}\n")
    (root / "config.yaml").write_text("synthetic: true\n")

    original = Path(__file__).resolve().parents[1] / "scripts/rollback.sh"
    # Rewrite only the fixed systemd directory to keep this test isolated.
    (scripts / "rollback.sh").write_text(
        original.read_text().replace(
            "/etc/systemd/system/", "${RPG_TEST_UNIT_DIR}/"
        )
    )
    (scripts / "lib.sh").write_text(
        """
set -Eeuo pipefail
RPG_TEST_ROOT="${RPG_ROLLBACK_TEST_ROOT:?}"
RPG_TEST_UNIT_DIR="${RPG_TEST_ROOT}/units"
RPG_STATE_ROOT="${RPG_TEST_ROOT}/state"
RPG_RELEASES_DIR="${RPG_TEST_ROOT}/releases"
RPG_CURRENT_LINK="${RPG_TEST_ROOT}/current"
RPG_WEB_CURRENT="${RPG_TEST_ROOT}/web-current"
RPG_CONFIG_PATH="${RPG_TEST_ROOT}/config.yaml"
rpg_require_root() { :; }
rpg_acquire_lock() { :; }
rpg_die() { printf 'ERROR: %s\\n' "$*" >&2; exit 1; }
rpg_assert_managed_path() { [[ "$1" == "${RPG_TEST_ROOT}/"* ]]; }
rpg_current_release() { readlink -f -- "${RPG_CURRENT_LINK}"; }
rpg_atomic_symlink() { ln -s -- "$1" "$2.new"; mv -Tf -- "$2.new" "$2"; }
rpg_assert_data_plane_unchanged() {
    diff -qr -- "$1/src/retailprintguard/proxy" "$2/src/retailprintguard/proxy" >/dev/null
}
rpg_restart_installed_services() { printf 'FORBIDDEN: default restart\\n' >&2; return 1; }
rpg_note() { printf '%s\\n' "$*"; }
"""
    )
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        """#!/bin/bash
set -Eeuo pipefail
case "$1" in
    show)
        service="$2"
        case "$service" in
            retailprintguard-pos-proxy.service) pid=1001 ;;
            retailprintguard-rch-proxy.service) pid=1002 ;;
            retailprintguard-epson-proxy.service) pid=1003 ;;
            *) exit 2 ;;
        esac
        if [[ "$*" == *'--value'* ]]; then printf '%s\\n' "$pid"; else
            printf 'Id=%s\\nActiveState=active\\nMainPID=%s\\n' "$service" "$pid"
            printf 'InvocationID=synthetic-%s\\n' "$pid"
            printf 'ExecMainStartTimestampMonotonic=123456\\n'
        fi
        ;;
    is-active) exit 0 ;;
    is-enabled)
        [[ "$2" != retailprintguard-parser.service ]]
        ;;
    restart|reload)
        printf '%s %s\\n' "$1" "$2" >>"${RPG_ROLLBACK_TEST_LOG:?}"
        if [[ "$1" == restart && "$2" == retailprintguard-parser.service &&
              "${RPG_ROLLBACK_TEST_FAIL_RESTART:-}" == yes ]]; then
            exit 1
        fi
        ;;
    *) exit 2 ;;
esac
"""
    )
    systemctl.chmod(0o755)
    ss = bin_dir / "ss"
    ss.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        "'LISTEN 0 128 127.0.0.1:9001 0.0.0.0:* users:((\"p\",pid=1001,fd=3))' "
        "'LISTEN 0 128 127.0.0.1:9002 0.0.0.0:* users:((\"p\",pid=1002,fd=3))' "
        "'LISTEN 0 128 127.0.0.1:9003 0.0.0.0:* users:((\"p\",pid=1003,fd=3))'\n"
    )
    ss.chmod(0o755)

    log = root / "service-actions.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "RPG_ROLLBACK_TEST_ROOT": str(root),
        "RPG_ROLLBACK_TEST_LOG": str(log),
    }

    def run(*, fail_restart: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - trusted local test fixture
            [bash, str(scripts / "rollback.sh"), "--control-plane-only"],
            env={**env, "RPG_ROLLBACK_TEST_FAIL_RESTART": "yes" if fail_restart else "no"},
            text=True, capture_output=True, check=False,
        )

    # A changed relay must stop before app/web symlinks and service actions.
    relay = old / "src/retailprintguard/proxy/relay.py"
    relay.write_text("# different relay\n")
    rejected = run()
    assert rejected.returncode != 0
    assert (root / "current").resolve() == new
    assert (root / "web-current").resolve() == web / new_id
    assert (state / "previous-release").read_text().strip() == str(old)
    assert not log.exists()

    relay.write_text("# same relay\n")
    failed_restart = run(fail_restart=True)
    assert failed_restart.returncode != 0
    assert (root / "current").resolve() == new
    assert (root / "web-current").resolve() == web / new_id
    assert (state / "previous-release").read_text().strip() == str(old)
    assert all("proxy" not in action for action in log.read_text().splitlines())

    succeeded = run()
    assert succeeded.returncode == 0, succeeded.stderr
    assert (root / "current").resolve() == old
    assert (root / "web-current").resolve() == web / old_id
    assert (state / "previous-release").read_text().strip() == str(new)
    actions = log.read_text().splitlines()
    assert actions
    assert all("proxy" not in action for action in actions)
    assert any(action == "reload nginx.service" for action in actions)
    assert any(action == "restart retailprintguard-parser.service" for action in actions)
