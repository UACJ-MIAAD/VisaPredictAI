"""The public stdlib CLI must work without an editable project installation."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("outside", [False, True], ids=["repo-cwd", "unrelated-cwd"])
def test_contracts_script_without_site_packages(tmp_path: Path, outside: bool) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "tools/check_contracts.py")],
        cwd=tmp_path if outside else ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contratos OK" in result.stdout


def test_contracts_module_without_site_packages() -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-S", "-m", "tools.check_contracts"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contratos OK" in result.stdout
