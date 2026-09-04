"""Shell-level checks for adb discovery used by mobile launchers."""

import os
import subprocess
from pathlib import Path


PREFLIGHT = (
    Path(__file__).resolve().parents[2] / "mobile" / "scripts" / "lib" / "preflight.sh"
)


def test_conda_adbutils_binary_is_added_to_path(tmp_path):
    adb = (
        tmp_path
        / "conda"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "adbutils"
        / "binaries"
        / "adb"
    )
    adb.parent.mkdir(parents=True)
    adb.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(adb, 0o755)

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDA_PREFIX": str(tmp_path / "conda"),
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; preflight_check_adb; command -v adb',
            "bash",
            str(PREFLIGHT),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == str(adb)
