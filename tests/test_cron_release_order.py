"""Execute the blocking cron gate: seal current bytes before validating them."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/freeze_and_rebuild.yml"
ORDER = [
    "experiments/build_release_manifest.py",
    "tools/refresh_monthly_views.py",
    "experiments/make_mlops_architecture.py",
    "tools/check_published_benchmark.py",
    "tools/check_contracts.py",
    "tools/check_consistency.py",
]


def _run(tmp_path: Path, fail_at: str = "") -> tuple[subprocess.CompletedProcess[str], list[str]]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["update"]["steps"] if s.get("id") == "release")
    script = step["run"].split("{\n", 1)[1].split("} > /tmp/release_gate.txt", 1)[0]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    command = bindir / "python"
    command.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$1" >> "$TRACE"\n'
        '[ "$1" != "$FAIL_AT" ] || exit 27\n'
        'case "$1" in\n'
        ' experiments/build_release_manifest.py) printf "fresh" > "$STATE";;\n'
        ' tools/check_contracts.py) [ "$(cat "$STATE")" = "fresh" ] || exit 28;;\n'
        "esac\n"
    )
    command.chmod(0o755)
    state = tmp_path / "state"
    state.write_text("previous-month")
    trace = tmp_path / "trace"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{bindir}:/usr/bin:/bin",
            "STATE": str(state),
            "TRACE": str(trace),
            "FAIL_AT": fail_at,
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result, trace.read_text().splitlines()


def test_release_gate_validates_the_new_seal_instead_of_the_previous_month(tmp_path: Path) -> None:
    result, calls = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == ORDER


@pytest.mark.parametrize(
    "failed",
    ORDER,
)
def test_release_gate_stops_at_each_failure(tmp_path: Path, failed: str) -> None:
    result, calls = _run(tmp_path, failed)
    assert result.returncode != 0
    assert calls[-1] == failed
    assert len(calls) == ORDER.index(failed) + 1
