"""C2: ejecutar los entrypoints reales sin venv por nombre ni documental hermano."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sync_clone(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    (repo / "experiments").mkdir(parents=True)
    (repo / "tools").mkdir()
    shutil.copy2(ROOT / "experiments/sync_all.sh", repo / "experiments/sync_all.sh")
    runtime = ROOT / "tools/publication_runtime.py"
    if runtime.exists():
        shutil.copy2(runtime, repo / "tools/publication_runtime.py")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _sync(repo: Path, python: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "experiments/sync_all.sh", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "VP_PUBLISH_PYTHON": python,
            "VP_TRACKING_PYTHON": python,
            "VP_DVC": "/usr/bin/true",
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "experiments"))),
            "SYNC_PUBLISH": "0",
        },
    )


def test_publish_uses_explicit_interpreter_without_ante_nf(sync_clone: Path) -> None:
    from tests.conftest import requiere

    requiere(modulos=("darts",))
    result = _sync(sync_clone, sys.executable, "--publish")
    # El intérprete sí puede acreditar. La siguiente puerta rechaza la campaña ausente.
    assert result.returncode == 7, result.stdout + result.stderr
    assert "preflight publish: OK" in result.stdout
    assert not (sync_clone / "reports").exists()


def test_missing_interpreter_fails_before_effects(sync_clone: Path) -> None:
    result = _sync(sync_clone, "/no/such/python", "--publish")
    assert result.returncode == 12, result.stdout + result.stderr
    assert "VP_PUBLISH_PYTHON" in result.stderr
    assert not (sync_clone / "reports").exists()


def test_unexpected_arguments_fail_before_preparation(sync_clone: Path) -> None:
    result = _sync(sync_clone, sys.executable, "--unknown")
    assert result.returncode == 2, result.stdout + result.stderr
    assert not (sync_clone / "reports").exists()


def test_incomplete_interpreter_fails_before_effects(sync_clone: Path) -> None:
    wrapper = sync_clone / "stdlib-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    wrapper.chmod(0o755)
    result = _sync(sync_clone, str(wrapper), "--publish")
    assert result.returncode == 12, result.stdout + result.stderr
    assert "preflight publish: ERROR" in result.stderr and "ModuleNotFoundError" in result.stderr
    assert not (sync_clone / "reports").exists()


def test_tracking_profile_is_checked_before_mlflow_or_dvc(sync_clone: Path) -> None:
    wrapper = sync_clone / "stdlib-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    wrapper.chmod(0o755)
    result = _sync(sync_clone, str(wrapper))
    assert result.returncode == 12 and "preflight prepare: ERROR" in result.stderr
    assert not (sync_clone / "mlflow.db").exists()
    assert not (sync_clone / "reports").exists()


@pytest.mark.parametrize("name", ["test_f5_paper_rules.py", "test_tex_tables.py"])
def test_absence_declared_skips_collection_but_present_bad_file_fails(tmp_path: Path, name: str) -> None:
    if name == "test_tex_tables.py":
        from tests.conftest import requiere

        requiere(modulos=("statsmodels",))
    repo = tmp_path / "copy"
    (repo / "tests").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "reports/governance").mkdir(parents=True)
    shutil.copy2(ROOT / "tests" / name, repo / "tests" / name)
    shutil.copy2(ROOT / "tools/consistency_rules.yml", repo / "tools/consistency_rules.yml")
    shutil.copy2(ROOT / "reports/governance/key_facts.json", repo / "reports/governance/key_facts.json")
    documental = tmp_path / "documental"
    env = {
        **os.environ,
        "VP_LATEX_DIR": str(documental),
        "VP_ALLOW_MISSING_LATEX": "1",
        "PYTHONPATH": os.pathsep.join(map(str, (ROOT, ROOT / "tools", ROOT / "experiments"))),
    }
    command = [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-rs", str(repo / "tests" / name)]
    absent = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert absent.returncode == 5 and "1 skipped" in absent.stdout, absent.stdout + absent.stderr
    # Una carpeta presente con una ruta ilegible NO equivale a un hermano ausente.
    relative = "reports/paper_micai/paper.tex" if "f5" in name else "reports/latex/ProyectoI_VisaPredictAI.tex"
    (documental / relative).mkdir(parents=True)
    broken = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert broken.returncode == 2 and "IsADirectoryError" in broken.stdout, broken.stdout + broken.stderr
    (documental / relative).rmdir()
    incomplete = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert incomplete.returncode == 2 and "FileNotFoundError" in incomplete.stdout
    shutil.rmtree(documental)
    documental.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    dangling = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert dangling.returncode == 2 and "FileNotFoundError" in dangling.stdout
    documental.unlink()
    env.pop("VP_ALLOW_MISSING_LATEX")
    undeclared = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert undeclared.returncode == 2 and "FileNotFoundError" in undeclared.stdout


def test_documental_stat_permission_error_is_not_absence(tmp_path: Path, monkeypatch) -> None:
    from tests.latex_support import read_documental

    root = tmp_path / "documental"
    root.mkdir()
    real = Path.lstat

    def denied(path: Path):
        if path == root:
            raise PermissionError("denied documental")
        return real(path)

    monkeypatch.setenv("VP_ALLOW_MISSING_LATEX", "1")
    monkeypatch.setattr(Path, "lstat", denied)

    def unexpected_skip(*args, **kwargs):
        pytest.fail("un error de permisos no se puede convertir en omisión")

    monkeypatch.setattr("tests.latex_support.pytest.skip", unexpected_skip)
    with pytest.raises(PermissionError, match="denied documental"):
        read_documental(root / "paper.tex", root)
