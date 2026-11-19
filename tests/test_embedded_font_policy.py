"""A reviewed font must not turn the private-evidence gate into a binary bypass."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def test_secret_gate_allows_only_the_reviewed_font_and_rejects_tampering(tmp_path):
    root = Path(__file__).resolve().parents[1]
    sandbox = tmp_path / "repository"
    scripts = sandbox / "scripts"
    fonts = sandbox / "src/retailprintguard/render/fonts"
    scripts.mkdir(parents=True)
    fonts.mkdir(parents=True)
    shutil.copyfile(root / "scripts/check_secrets.sh", scripts / "check_secrets.sh")
    original = root / "src/retailprintguard/render/fonts/DejaVuSansMono.ttf"
    target = fonts / original.name
    shutil.copyfile(original, target)
    shutil.copyfile(original.with_name("LICENSE-DejaVu.txt"), fonts / "LICENSE-DejaVu.txt")
    subprocess.run(["git", "init", "-q", str(sandbox)], check=True)  # noqa: S603, S607

    def scan():
        import os
        env = dict(os.environ, PYTHON=sys.executable)
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(scripts / "check_secrets.sh")],
            cwd=sandbox, env=env, capture_output=True, text=True, check=False,
        )

    assert scan().returncode == 0
    target.write_bytes(target.read_bytes() + b"UNREVIEWED")
    failed = scan()
    assert failed.returncode == 1
    assert "unverified embedded font" in failed.stderr
    shutil.copyfile(original, target)
    shutil.copyfile(original, fonts / "other.ttf")
    failed = scan()
    assert failed.returncode == 1
    assert "unexpected binary candidate" in failed.stderr
    (fonts / "other.ttf").unlink()
    (fonts / "LICENSE-DejaVu.txt").write_text("incomplete license")
    failed = scan()
    assert failed.returncode == 1
    assert "unverified embedded font" in failed.stderr
    (fonts / "LICENSE-DejaVu.txt").unlink()
    failed = scan()
    assert failed.returncode == 1
    assert "unverified embedded font" in failed.stderr
