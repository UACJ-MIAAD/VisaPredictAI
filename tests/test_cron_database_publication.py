"""A monthly database rebuild must update its lock and upload its cached output."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.check_dvc_lock_fresh import resolve_dvc

ROOT = Path(__file__).resolve().parents[1]


def _run_script() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/freeze_and_rebuild.yml").read_text())
    return next(step["run"] for step in workflow["jobs"]["update"]["steps"] if step.get("id") == "run")


@pytest.mark.parametrize("changed", [False, True])
def test_real_database_lock_follows_the_panel_before_publishing(tmp_path: Path, changed: bool) -> None:
    dvc = resolve_dvc(ROOT, os.environ)
    if dvc is None:
        pytest.skip("Governed DVC absent in the dev profile; exercised in model CI")

    def call(*args: str) -> str:
        return subprocess.check_output([str(dvc), *args], cwd=tmp_path, text=True, stderr=subprocess.STDOUT)

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    call("init", "-q")
    stages = {
        "scrape": {"cmd": "true", "deps": ["source.txt"], "outs": [{"raw.csv": {"cache": False}}]},
        "panel": {"cmd": "true", "deps": ["raw.csv"], "outs": [{"panel.csv": {"cache": False}}]},
        "bulletins": {"cmd": "true", "deps": ["panel.csv"], "outs": [{"bulletins.json": {"cache": False}}]},
        "database": {"cmd": "true", "deps": ["raw.csv", "panel.csv"], "outs": ["panel.parquet"]},
    }
    (tmp_path / "dvc.yaml").write_text(yaml.safe_dump({"stages": stages}))
    for name in ("source.txt", "raw.csv", "panel.csv", "bulletins.json", "panel.parquet"):
        (tmp_path / name).write_text("previous month\n")
    call("commit", "--force")
    if changed:
        for name in ("raw.csv", "panel.csv", "bulletins.json", "panel.parquet"):
            (tmp_path / name).write_text("new month\n")
    line = next(
        line.strip() for line in _run_script().splitlines() if line.strip().startswith("dvc commit --force scrape")
    )
    call(*shlex.split(line)[1:])
    assert json.loads(call("status", "--json", "database")) == {}


@pytest.mark.parametrize("transport_rc", [0, 31])
def test_cache_transport_is_required_before_the_data_commit(tmp_path: Path, transport_rc: int) -> None:
    script = _run_script()
    start = script.index('pip install "dvc==')
    end = script.index('publish_data "$msg"', start) + len('publish_data "$msg"')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {
        "pip": "exit 0\n",
        "python": "echo 2026-10\n",
        "dvc": 'echo "dvc $*" >> "$TRACE"\n[ "$1" != push ] || exit "$TRANSPORT_RC"\n',
    }.items():
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    trace = tmp_path / "trace"
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            'publish_data() { echo PUBLISHED >> "$TRACE"; }; new=0; source_status=blocked;\n' + script[start:end],
        ],
        env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "TRACE": str(trace), "TRANSPORT_RC": str(transport_rc)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    events = trace.read_text().splitlines()
    assert "dvc push database" in events, events
    assert (result.returncode == 0) == (transport_rc == 0), result.stderr
    assert ("PUBLISHED" in events) == (transport_rc == 0), events
