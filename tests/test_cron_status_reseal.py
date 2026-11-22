"""Un commit solo-de-estado del feed de ingesta re-sella el release (16-nov-2026).

El feed `reports/governance/ingestion_state.json` viaja sellado en el manifiesto. El cron lo
publicaba sin rebuild y sin re-sellar: `check_contracts` quedó rojo en main y el web sirvió
`stale` («size 367 != 350»). Estas pruebas EJECUTAN los fragmentos reales del workflow.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/freeze_and_rebuild.yml"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _step(job: str, key: str, value: str) -> dict:
    return next(s for s in _jobs()[job]["steps"] if s.get(key) == value)


def _helpers() -> str:
    script = _step("update", "id", "run")["run"]
    begin = script.index("publish_data() {")
    end = script.index("# A3: skip_freeze omite SOLO")
    return script[begin:end]


def _run(tmp_path: Path, staged_changes: bool) -> tuple[str, str]:
    """Corre publish_data + request_reseal reales con git/pip/python falsos."""
    binary = tmp_path / "bin"
    binary.mkdir()
    trace = tmp_path / "trace"
    for tool in ("python", "pip", "bash"):
        (binary / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "$TRACE"\n')
    # `git diff --cached --quiet` sale 1 si hay algo stageado; todo lo demás pasa.
    (binary / "git").write_text(
        '#!/bin/sh\necho "git $*" >> "$TRACE"\n'
        'case "$*" in "diff --cached --quiet") [ "$STAGED" = 1 ] && exit 1 || exit 0;; '
        '"rev-parse HEAD") echo abc123;; esac\n'
    )
    for stub in binary.iterdir():
        stub.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    result = subprocess.run(
        [
            "/bin/bash",
            "-e",
            "-c",
            _helpers() + '\npublish_data "Record source ingestion state: blocked"\nrequest_reseal\n',
        ],
        env={
            **os.environ,
            "PATH": f"{binary}:/usr/bin:/bin",
            "TRACE": str(trace),
            "STAGED": "1" if staged_changes else "0",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REF_NAME": "main",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return output.read_text(), trace.read_text() if trace.exists() else ""


def test_pushed_status_commit_requests_a_reseal_with_dev_deps(tmp_path: Path) -> None:
    output, trace = _run(tmp_path, staged_changes=True)
    assert "pushed=true" in output and "reseal=true" in output
    assert "pip install --require-hashes -r locks/dev-linux-x86_64.txt" in trace


def test_unchanged_feed_requests_nothing(tmp_path: Path) -> None:
    output, trace = _run(tmp_path, staged_changes=False)
    assert "reseal" not in output and "pushed" not in output
    assert "pip install" not in trace


def test_both_status_only_exits_request_the_reseal() -> None:
    script = _step("update", "id", "run")["run"]
    lines = [ln.strip() for ln in script.splitlines()]
    publishes = [
        i for i, ln in enumerate(lines) if ln == 'publish_data "Record source ingestion state: $source_status"'
    ]
    assert len(publishes) == 2
    assert all(lines[i + 1] == "request_reseal" for i in publishes)


def test_release_gate_seals_on_reseal_even_after_a_dead_freeze() -> None:
    cond = _step("update", "id", "release")["if"]
    assert "(always() && steps.run.outputs.reseal == 'true')" in cond
    # El rebuild sigue exigiendo EDA y modelo verdes.
    assert "success() && steps.run.outputs.rebuilt == 'true'" in cond
    assert "steps.eda.outcome == 'success'" in cond and "steps.model.outcome == 'success'" in cond


def test_reseal_reaches_delivery_and_verification() -> None:
    assert _jobs()["update"]["outputs"]["reseal"] == "${{ steps.run.outputs.reseal }}"
    redeploy = _step("delivery", "name", "Redeploy the website on a rebuild (Netlify build hook)")["if"]
    verify = _step("delivery", "name", "Verify the deploy actually served this run's release (C4/A-01)")["if"]
    for cond in (redeploy, verify):
        assert "needs.update.outputs.reseal == 'true'" in cond
        assert "needs.update.outputs.release_outcome == 'success'" in cond
        assert "steps.ciwait.outcome == 'success'" in cond
