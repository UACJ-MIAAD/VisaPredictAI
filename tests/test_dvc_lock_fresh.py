"""Tests del gate pre-push ``dvc-lock-fresh`` (D1): contrato fail-closed, igualdad con CI/E2 y
una mutación AISLADA (copia temporal del DAG git-only) que demuestra el RED sin tocar el worktree.

Los tests que necesitan el binario DVC gobernado se omiten donde no existe (el job base de CI no
instala dvc; E2 lo instala solo en su propio paso). Los tests de lógica corren en todas partes.
"""

from __future__ import annotations

import json
import os
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


def test_ci_e2_delegates_to_this_gate_instead_of_reimplementing_it() -> None:
    """CI ya no repite la comprobación en bash: invoca ESTE módulo, así que no pueden divergir.

    Antes había dos implementaciones —una lista de targets en el YAML y otra en Python— atadas
    sólo por una prueba de igualdad. Ahora el paso corre el checker, y lo que se exige es
    precisamente eso: que lo invoque, que le pase el DVC del runner y que NO quede ningún
    `dvc status` suelto evaluando el lock por su cuenta.
    """
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    paso = ci[ci.index("DVC lock matches the committed pipeline (E2)") :]
    paso = paso[: paso.index("\n      - name:")]
    assert "tools/check_dvc_lock_fresh.py" in paso, "el paso E2 debe invocar el checker"
    assert f"{gate.DVC_ENV}=" in paso, "debe pasarle el DVC del runner por $VP_DVC"
    assert "dvc status" not in paso, "no puede quedar una segunda implementación en bash"


def test_watched_and_excluded_stages_are_explicit() -> None:
    """`database` entra (#57) y `scrape` sigue fuera; ambas decisiones son declaradas."""
    assert "database" in gate.STAGES
    assert "scrape" not in gate.STAGES and "scrape" in gate.EXCLUDED_STAGES
    assert gate.CACHE_BACKED_STAGES == frozenset({"database"})
    # el DAG real no tiene más stages que los vigilados o los excluidos a propósito
    dag = yaml.safe_load((ROOT / "dvc.yaml").read_text(encoding="utf-8"))["stages"]
    assert set(dag) == set(gate.STAGES) | gate.EXCLUDED_STAGES


def test_the_database_stage_is_built_before_the_gate_runs_in_ci() -> None:
    """La razón por la que `database` PUEDE vigilarse en CI: el paso anterior lo construye."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert ci.index("pipeline.build_database") < ci.index("DVC lock matches the committed pipeline (E2)")


# --- la excepción de caché: acotada, y probada en los dos sentidos -------------------------
_PARQUET = "data/processed/visa_panel_long.parquet"


def test_cache_only_residue_of_database_is_tolerated(tmp_path: Path) -> None:
    """En un clon sin caché DVC, el out de `database` sale `not in cache` y eso NO es desfase.

    Medido: con la caché vacía DVC responde ese mismo texto tanto si el parquet es correcto como
    si está corrupto o ausente, así que ahí la línea no lleva información; lo que sí la lleva, y
    se sigue exigiendo, son sus `changed deps`.
    """
    root, env = _fake_dvc(tmp_path)
    salida = json.dumps({"database": [{"changed outs": {_PARQUET: gate.NOT_IN_CACHE}}]})
    ok, message = gate.evaluate(root, _fake_runner(salida), env)
    assert ok and "sin caché local" in message and "DEPS sí quedan verificadas" in message


@pytest.mark.parametrize(
    "salida,motivo",
    [
        ({"database": [{"changed deps": {"vp_data/config.py": "modified"}}]}, "una dep desfasada"),
        (
            {
                "database": [
                    {"changed deps": {"vp_data/config.py": "modified"}},
                    {"changed outs": {_PARQUET: gate.NOT_IN_CACHE}},
                ]
            },
            "dep desfasada aunque falte la caché",
        ),
        ({"database": [{"changed outs": {_PARQUET: "modified"}}]}, "out modificado de verdad"),
        ({"database": [{"changed outs": {_PARQUET: "deleted"}}]}, "out borrado"),
        (
            {"database": [{"changed outs": {_PARQUET: gate.NOT_IN_CACHE, "otro.parquet": "modified"}}]},
            "un out tolerable no arrastra a otro que no lo es",
        ),
        ({"panel": [{"changed outs": {"x": gate.NOT_IN_CACHE}}]}, "la excepción NO se extiende a otros stages"),
        ({"database": [{"changed outs": {}}]}, "forma vacía"),
        ({"database": [{"changed outs": {_PARQUET: gate.NOT_IN_CACHE}, "changed deps": {"a": "b"}}]}, "clave extra"),
        ({"database": "texto"}, "forma inesperada"),
        ({"database": []}, "lista vacía"),
    ],
)
def test_anything_beyond_the_cache_residue_still_fails(tmp_path: Path, salida: dict, motivo: str) -> None:
    """La tolerancia no puede crecer por accidente: todo lo demás sigue bloqueando."""
    root, env = _fake_dvc(tmp_path)
    ok, message = gate.evaluate(root, _fake_runner(json.dumps(salida)), env)
    assert not ok, f"debía bloquear: {motivo}"
    assert "desfasado" in message


def test_red_the_drift_that_this_gate_was_blind_to(tmp_path: Path) -> None:
    """RED de #57: el desfase REAL que vivió en `main` semanas sin que nada lo viera.

    Los dos hashes son los que `dvc.lock` arrastraba desde M49/C8 hasta M72-R1. Con `database`
    fuera del gate, este estado salía verde; ahora bloquea.
    """
    root, env = _fake_dvc(tmp_path)
    salida = json.dumps(
        {"database": [{"changed deps": {"pipeline/db_migrations.py": "modified", "vp_data/config.py": "modified"}}]}
    )
    ok, message = gate.evaluate(root, _fake_runner(salida), env)
    assert not ok
    assert "database" in message and "db_migrations" in message
    # y con el stage fresco, el mismo gate pasa: el control benigno del RED
    ok_fresco, _ = gate.evaluate(root, _fake_runner("{}"), env)
    assert ok_fresco


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
def test_isolated_database_drift_turns_red_and_the_old_target_list_missed_it(
    dag_copy: tuple[Path, dict[str, str]],
) -> None:
    """RED REAL de #57, con el DVC de verdad sobre una copia aislada del DAG.

    Se ensucia una dep de `database` y se compara el gate de hoy con la lista de targets que
    tenía antes de M72-R1. El desfase que vivió semanas en `main` **salía verde** con aquella
    lista; con ésta, bloquea. La comparación es lo que hace la prueba discriminante: sin ella
    sólo se estaría comprobando que un `dvc status` no vacío falla, que ya era cierto.
    """
    repo, env = dag_copy
    dep = repo / "vp_data/config.py"
    dep.write_text(dep.read_text(encoding="utf-8") + "\n# mutación aislada del stage database\n", encoding="utf-8")

    ok, msg = gate.evaluate(repo, gate.default_runner, env)
    assert not ok, "el gate de hoy debe cazar el desfase de database"
    assert "database" in msg and "config.py" in msg and "make repro" in msg

    # la lista ANTERIOR (sin `database`) sobre el MISMO árbol sucio: verde, que es el defecto
    anterior = tuple(x for x in gate.STAGES if x != "database")
    original, gate.STAGES = gate.STAGES, anterior
    try:
        ok_antes, _ = gate.evaluate(repo, gate.default_runner, env)
    finally:
        gate.STAGES = original
    assert ok_antes, "con la lista anterior el desfase pasaba desapercibido: ése era el agujero"

    # el worktree real no se tocó
    assert "mutación aislada" not in (ROOT / "vp_data/config.py").read_text(encoding="utf-8")


@needs_dvc
def test_isolated_copy_without_cache_reports_only_the_tolerated_residue(
    dag_copy: tuple[Path, dict[str, str]],
) -> None:
    """La copia aislada NO tiene caché DVC, así que ejercita el residuo tolerado de verdad."""
    repo, env = dag_copy
    salida = gate.default_runner([str(REAL_DVC), "status", "--json", "database"], repo)
    estado = json.loads(salida.stdout)
    assert estado == {} or gate._is_cache_only_residue("database", estado.get("database")), estado
    ok, _ = gate.evaluate(repo, gate.default_runner, env)
    assert ok


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
