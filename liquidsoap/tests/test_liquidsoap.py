import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script_name", ["station.liq", "live.liq"])
def test_liquidsoap_script_passes_installed_checker(script_name):
    liquidsoap = shutil.which("liquidsoap")
    if liquidsoap is None:
        pytest.skip("liquidsoap is not installed")

    env = os.environ.copy()
    env.update(
        {
            "RADIO_ROOT": str(ROOT),
            "ICECAST_SOURCE_PASSWORD": "test-placeholder",
        }
    )
    result = subprocess.run(
        [liquidsoap, "--check", str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

