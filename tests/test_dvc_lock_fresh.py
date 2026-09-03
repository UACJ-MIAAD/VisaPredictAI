"""Tests del gate pre-push ``dvc-lock-fresh`` (D1): contrato fail-closed, igualdad con CI/E2 y
una mutación AISLADA (copia temporal del DAG git-only) que demuestra el RED sin tocar el worktree.

Los tests que necesitan el binario DVC gobernado se omiten donde no existe (el job base de CI no
instala dvc; E2 lo instala solo en su propio paso). Los tests de lógica corren en todas partes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from tools import check_dvc_lock_fresh as gate

ROOT = Path(__file__).resolve().parent.parent
REAL_DVC = gate.resolve_dvc(ROOT, os.environ)
needs_dvc = pytest.mark.skipif(REAL_DVC is None, reason="DVC gobernado (ante/bin/dvc o $VP_DVC) ausente")


class _FakeRunner:
    """Runner inyectable: devuelve una salida fija y registra las llamadas."""

    def __init__(self, stdout: str = "{}", returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), self.returncode, self.stdout, self.stderr)


def _fake_runner(stdout: str = "{}", returncode: int = 0, stderr: str = "") -> _FakeRunner:
    return _FakeRunner(stdout, returncode, stderr)


def _fake_dvc(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    exe = tmp_path / "fake-dvc"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return tmp_path, {gate.DVC_ENV: str(exe)}


def test_empty_status_passes(tmp_path: Path) -> None:
    root, env = _fake_dvc(tmp_path)
    runner = _fake_runner("{}")
    ok, msg = gate.evaluate(root, runner, env)
    assert ok and msg.startswith("✓")
    assert runner.calls[0][1:] == ["status", "--json", *gate.STAGES]


def test_stale_stage_fails_and_names_it(tmp_path: Path) -> None:
    root, env = _fake_dvc(tmp_path)
    stale = '{"key_facts": [{"changed outs": {"reports/governance/key_facts.json": "modified"}}]}'
    ok, msg = gate.evaluate(root, _fake_runner(stale), env)
    assert not ok and "key_facts" in msg and "make repro" in msg


def test_nonzero_dvc_exit_fails(tmp_path: Path) -> None:
    root, env = _fake_dvc(tmp_path)
    ok, msg = gate.evaluate(root, _fake_runner("{}", returncode=1, stderr="boom"), env)
    assert not ok and "código 1" in msg and "boom" in msg


@pytest.mark.parametrize("stdout", ["", "not json", "[]", '"{}"', "null", "1"])
def test_invalid_or_non_object_json_fails(tmp_path: Path, stdout: str) -> None:
    root, env = _fake_dvc(tmp_path)
    ok, msg = gate.evaluate(root, _fake_runner(stdout), env)
    assert not ok and msg.startswith("✗")


def test_missing_governed_dvc_fails_closed(tmp_path: Path) -> None:
    ok, msg = gate.evaluate(tmp_path, _fake_runner("{}"), {})
    assert not ok and "fail-closed" in msg


def test_targets_match_ci_e2_exactly() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    m = re.search(r"dvc status --json ([a-z_ ]+?)\)", ci)
    assert m, "paso E2 no encontrado en ci.yml"
    assert m.group(1).split() == list(gate.STAGES)
    assert "scrape" not in gate.STAGES and "database" not in gate.STAGES


def test_hook_is_pre_push_only_with_expected_flags() -> None:
    cfg = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in cfg["repos"] for h in repo.get("hooks", []) if h.get("id") == "dvc-lock-fresh"]
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook["stages"] == ["pre-push"]
    assert hook["always_run"] is True and hook["pass_filenames"] is False
    assert hook["entry"].endswith("tools/check_dvc_lock_fresh.py")


# ---------------------------------------------------------------------------
# Copia aislada del DAG git-only: mismo dvc.yaml/dvc.lock y los mismos deps/outs versionados,
# en un repo DVC temporal sin SCM. Nada de esto toca el worktree real.
# ---------------------------------------------------------------------------
def _dag_paths() -> set[str]:
    lock = yaml.safe_load((ROOT / "dvc.lock").read_text(encoding="utf-8"))
    paths: set[str] = set()
    for stage in gate.STAGES:
        entry = lock["stages"][stage]
        for item in entry.get("deps", []) + entry.get("outs", []):
            paths.add(item["path"])
    return paths


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


@pytest.fixture(scope="module")
def pristine_dag(tmp_path_factory: pytest.TempPathFactory) -> Path:
    assert REAL_DVC is not None
    base = tmp_path_factory.mktemp("dag") / "repo"
    base.mkdir()
    for name in ("dvc.yaml", "dvc.lock", ".dvcignore"):
        if (ROOT / name).exists():
            _copy(ROOT / name, base / name)
    for rel in sorted(_dag_paths()):
        _copy(ROOT / rel, base / rel)
    subprocess.run([str(REAL_DVC), "init", "--no-scm", "-f", "-q"], cwd=base, check=True, capture_output=True)
    return base


@pytest.fixture
def dag_copy(pristine_dag: Path, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    shutil.copytree(pristine_dag, repo, symlinks=True)
    return repo, {gate.DVC_ENV: str(REAL_DVC)}


@needs_dvc
def test_isolated_copy_is_fresh(dag_copy: tuple[Path, dict[str, str]]) -> None:
    repo, env = dag_copy
    ok, msg = gate.evaluate(repo, gate.default_runner, env)
    assert ok, msg


@needs_dvc
def test_isolated_mutation_turns_red(dag_copy: tuple[Path, dict[str, str]]) -> None:
    repo, env = dag_copy
    dep = repo / "pipeline/build_bulletins_json.py"
    dep.write_text(dep.read_text(encoding="utf-8") + "\n# mutación aislada para el gate D1\n", encoding="utf-8")
    ok, msg = gate.evaluate(repo, gate.default_runner, env)
    assert not ok and "bulletins" in msg and "make repro" in msg
    assert "mutación aislada" not in (ROOT / "pipeline/build_bulletins_json.py").read_text(encoding="utf-8")


@needs_dvc
def test_isolated_benign_change_is_not_flagged(dag_copy: tuple[Path, dict[str, str]]) -> None:
    repo, env = dag_copy
    (repo / "NOTAS_fuera_del_dag.md").write_text("cambio benigno: no es dep ni out de ningún stage\n")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs/README_local.md").write_text("otro archivo fuera del DAG\n")
    ok, msg = gate.evaluate(repo, gate.default_runner, env)
    assert ok, msg


@needs_dvc
def test_real_worktree_passes() -> None:
    ok, msg = gate.evaluate(ROOT, gate.default_runner, os.environ)
    assert ok, msg
