"""Stage each generated TeX export with its JSON owner, never a same-prefix stray."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import cron_publish as cp


@pytest.mark.parametrize(
    ("stage", "relative"),
    [
        ("model", "reports/latex/key_facts.tex"),
        ("eda", "reports/latex/fe_facts.tex"),
        ("release", "reports/release/release_manifest.json"),
        ("release", "docs/schema_er.svg"),
        ("release", "docs/mlops_architecture.svg"),
        ("release", "docs/MLOPS_ARCHITECTURE.md"),
    ],
)
def test_real_staging_owns_exact_generated_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, relative: str
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    stray = tmp_path / (relative + ".backup")
    for p in (target, stray):
        p.write_text("before\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Javier Rebull",
            "-c",
            "user.email=168046724+jrebull@users.noreply.github.com",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    for p in (target, stray):
        p.write_text("after\n")
    monkeypatch.setattr(cp, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["cron_publish.py", "--stage", stage])
    assert cp.main() == 0
    staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=tmp_path, text=True).splitlines()
    remaining = subprocess.check_output(["git", "diff", "--name-only"], cwd=tmp_path, text=True).splitlines()
    assert staged == [relative]
    assert remaining == [relative + ".backup"]
