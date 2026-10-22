"""A failed figure producer must stop EDA, not be hidden by Bash's if context."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/freeze_and_rebuild.yml"
COMMANDS = [
    "experiments/build_eda_facts.py",
    "-m vp_model.plots",
    "experiments/make_eda_figures.py",
    "experiments/make_gallery_figures.py",
    "experiments/build_eda_report.py",
    "tests/test_eda_facts.py",
    "tests/test_eda_report.py",
    "experiments/build_fe_facts.py",
    "experiments/make_fe_figures.py",
    "experiments/build_fe_report.py",
    "tests/test_fe_report.py",
]


def _step(name: str) -> dict:
    return next(s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["update"]["steps"] if s.get("id") == name)


def _execute(tmp_path: Path, failed: str) -> tuple[int, list[str]]:
    body = _step("eda")["run"].split("if ! run_eda; then", 1)[0]
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TRACE"\n[ "$*" != "$FAIL_AT" ] || exit 23\n')
    python.chmod(0o755)
    trace = tmp_path / "trace"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", body + "\nif ! run_eda; then exit 73; fi\n"],
        env={**os.environ, "PATH": f"{binary}:/usr/bin:/bin", "TRACE": str(trace), "FAIL_AT": failed},
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode, trace.read_text().splitlines()


@pytest.mark.parametrize("failed", COMMANDS)
def test_each_eda_failure_stops_before_later_producers(tmp_path: Path, failed: str) -> None:
    rc, calls = _execute(tmp_path, failed)
    assert rc != 0, failed
    assert calls == COMMANDS[: COMMANDS.index(failed) + 1]


def test_the_complete_benign_eda_pipeline_still_runs(tmp_path: Path) -> None:
    rc, calls = _execute(tmp_path, "")
    assert rc == 0 and calls == COMMANDS


def test_eda_failure_is_fatal_for_the_cut() -> None:
    assert not _step("eda").get("continue-on-error", False)
    assert "steps.eda.outcome == 'success'" in _step("release")["if"]


def test_canonical_documental_is_a_required_read_only_input() -> None:
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["update"]
    copies = [s for s in job["steps"] if s.get("with", {}).get("repository") == "UACJ-MIAAD/VisaPredictAI_LaTeX"]
    assert len(copies) == 1
    assert not copies[0].get("continue-on-error", False)
    assert copies[0]["with"]["persist-credentials"] is False
    assert job["env"]["VP_LATEX_DIR"] == "${{ github.workspace }}/latex_repo"


def test_preparation_branches_cannot_run_production_delivery() -> None:
    delivery = yaml.safe_load(WORKFLOW.read_text())["jobs"]["delivery"]
    assert "github.ref == 'refs/heads/main'" in delivery["if"]


@pytest.mark.parametrize("state", ["fresh", "stale"])
def test_incomplete_release_falls_through_to_the_complete_rebuild(tmp_path: Path, state: str) -> None:
    script = _step("run")["run"]
    begin = script.index("stale=$(python tools/check_ingestion.py --mode pending)")
    end = script.index("# 1. Rebuild CSVs/panel/DuckDB OFFLINE")
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text('#!/bin/sh\ncase "$*" in *model-pending*) echo "$MODEL_STATE";; *) echo fresh;; esac\n')
    python.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            "new=0; source_status=blocked; publish_data() { echo STATE; };\n"
            + script[begin:end]
            + "\necho FULL_REBUILD",
        ],
        env={
            **os.environ,
            "PATH": f"{binary}:/usr/bin:/bin",
            "MODEL_STATE": state,
            "GITHUB_OUTPUT": str(tmp_path / "output"),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert ("FULL_REBUILD" in result.stdout) == (state == "stale")
    assert ("STATE" in result.stdout) == (state == "fresh")
