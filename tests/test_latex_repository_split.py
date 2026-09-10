"""M66: el repo de datos ya no es un proyecto LaTeX, sin perder la regla cero."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_consistency as cc  # noqa: E402

RULES = yaml.safe_load((ROOT / "tools" / "consistency_rules.yml").read_text())
EXPORTS = tuple(RULES["latex_exports"])


def _tracked(*paths: str) -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "--", *paths], cwd=ROOT, text=True)
    return [line for line in out.splitlines() if line]


def test_only_the_five_pipeline_exports_remain_under_latex_paths() -> None:
    assert set(_tracked("reports/latex", "reports/paper_micai")) == set(EXPORTS)


def test_the_data_repo_no_longer_owns_a_latex_workflow_or_compile_gate() -> None:
    assert not (ROOT / ".github/workflows/latex.yml").exists()
    assert not (ROOT / "tools/check_latex_log.sh").exists()
    assert not (ROOT / "tests/test_latex_log_gate.py").exists()


def test_the_canonical_latex_repository_contains_all_three_entrypoints() -> None:
    assert cc.LATEX_DIR.is_dir(), f"repo LaTeX no montado: {cc.LATEX_DIR}"
    expected = (
        "reports/latex/ProyectoI_VisaPredictAI.tex",
        "reports/latex/AnteproyectoVisaPredictAI.tex",
        "reports/paper_micai/paper.tex",
    )
    assert all((cc.LATEX_DIR / path).is_file() for path in expected)


def test_all_pipeline_exports_are_byte_identical_in_both_repositories() -> None:
    assert len(EXPORTS) == 5
    assert cc._latex_export_violations(RULES) == []


def test_a_divergent_export_is_rejected(tmp_path: Path, monkeypatch) -> None:
    latex = tmp_path / "latex"
    for rel in EXPORTS:
        target = latex / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes())
    (latex / EXPORTS[0]).write_bytes(b"mutado\n")
    monkeypatch.setattr(cc, "LATEX_DIR", latex)
    problems = cc._latex_export_violations(RULES)
    assert problems == [f"LATEXSYNC  {EXPORTS[0]} diverge entre datos y repo LaTeX"]


def test_ci_mounts_the_latex_repository_as_a_required_dependency() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert workflow.count("repository: UACJ-MIAAD/VisaPredictAI_LaTeX") == 3
    assert (
        "continue-on-error: true\n        with:\n          repository: UACJ-MIAAD/VisaPredictAI_LaTeX" not in workflow
    )
    assert workflow.count("VP_LATEX_DIR") >= 3
