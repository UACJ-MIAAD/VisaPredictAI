"""M74-E-R4 · el gate del entorno dejaba pasar lo ungobernado y lo diferido.

La preparación de los entornos aislados destapó que `optuna` **no estaba en el perfil `model`** y
que el runbook corre el HPO con `$ANTE`. Peor que el hueco: **mis dos gates lo daban por bueno**.

* `check_env_matches_lock` clasificaba los extras como *informativos*. Lo justifiqué con `dvc` y
  `mlflow`, que son **herramientas**; `optuna` **participa en el cálculo**, y esa distinción no
  estaba en el gate. Instalarlo a mano habría dejado `reproduces_lock: true`.
* `check_entrypoint_smoke` no podía verlo: las seis importaciones de `optuna` son **diferidas
  dentro de funciones**, así que el módulo importa bien y el fallo llega horas después.

Y las tres líneas del HPO eran `run` (best-effort): habrían muerto con `ModuleNotFoundError` y el
runbook habría **seguido**, terminando en verde con los hiperparámetros sin re-derivar.
"""

from __future__ import annotations

import importlib.util
import json
import re
import stat
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from tools import check_entrypoint_smoke as ce  # noqa: E402
from tools import check_env_matches_lock as el  # noqa: E402

MODELADO = pytest.mark.skipif(importlib.util.find_spec("darts") is None, reason="extra `model` (darts) ausente")


def _venv(base: Path, nombre: str, dists: dict[str, str]) -> Path:
    venv = base / nombre
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(dists) + "\nEOF\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return venv


def _lock(base: Path, pines: dict[str, str]) -> Path:
    p = base / "lock.txt"
    p.write_text("\n".join(f"{k}=={v}" for k, v in pines.items()) + "\n", encoding="utf-8")
    return p


# ═══════════════════════════════ 1 · los extras dejan de ser decorativos
def test_un_extra_sin_declarar_BLOQUEA(tmp_path: Path) -> None:
    """★ Con el criterio de R2 esto daba `reproduces_lock: true` y `optuna` entraba al HPO."""
    v = _venv(tmp_path, "ante", {"torch": "2.13.0", "optuna": "4.9.0"})
    r = el.comparar(v, _lock(tmp_path, {"torch": "2.13.0"}))
    assert r["reproduces_lock"] is False
    assert r["undeclared"] == ["optuna"]


def test_los_extras_declarados_no_bloquean(tmp_path: Path) -> None:
    """`pip` es el instalador y el proyecto editable no puede pinnearse a sí mismo."""
    v = _venv(tmp_path, "ante", {"torch": "2.13.0", "pip": "26.1.2", "visapredictai": "1.0.0"})
    r = el.comparar(v, _lock(tmp_path, {"torch": "2.13.0"}))
    assert r["reproduces_lock"] is True and r["undeclared"] == []


def test_la_lista_de_excepciones_es_corta_y_por_interprete() -> None:
    """Si crece, se ve en el diff. Una excepción por intérprete, no una global."""
    assert set(el.UNGOVERNED_OK) == {"ante", "ante_nf"}
    assert el.UNGOVERNED_OK["ante"] == frozenset({"pip", "visapredictai"})
    assert el.UNGOVERNED_OK["ante_nf"] == frozenset({"pip"})


def test_el_proyecto_editable_no_se_permite_en_el_perfil_profundo(tmp_path: Path) -> None:
    """`ante_nf` no lo instala; si apareciera, sería el censo mirando la raíz en vez del intérprete."""
    v = _venv(tmp_path, "ante_nf", {"torch": "2.13.0", "visapredictai": "1.0.0"})
    r = el.comparar(v, _lock(tmp_path, {"torch": "2.13.0"}))
    assert r["undeclared"] == ["visapredictai"] and r["reproduces_lock"] is False


def test_el_censo_se_toma_DESDE_el_venv() -> None:
    """★ Heredando el cwd, el `visapredictai.egg-info` de la raíz salía como extra en LOS DOS
    intérpretes. Con los extras ya bloqueantes, ese falso positivo rompería el gate."""
    import ast

    fuente = (RAIZ / "tools" / "check_env_matches_lock.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    llamadas = [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run" and any(k.arg == "cwd" for k in n.keywords)
    ]
    assert llamadas, "el censo no fija `cwd`: volvería a medir el cwd y no el intérprete"


# ═══════════════════════════════ 2 · los imports diferidos
def test_los_terceros_se_recogen_aunque_esten_dentro_de_funciones(tmp_path: Path) -> None:
    """★ El caso de `optuna`: seis importaciones, todas dentro de funciones."""
    m = tmp_path / "m.py"
    m.write_text("import pandas\n\n\ndef f():\n    import optuna\n    return optuna\n", encoding="utf-8")
    assert ce._terceros_de(m) == {"pandas", "optuna"}


def test_la_recursion_NO_sigue_imports_locales_diferidos(tmp_path: Path) -> None:
    """★ Contraparte: seguirlos daba dos falsos positivos.

    `run_global_deep` alcanzaba `vp_model.dataset` —y con él `duckdb`, ausente del perfil
    profundo— sólo a través de `preprocess.demo()`, un autochequeo que ningún runner llama.
    """
    (tmp_path / "vp_model").mkdir()
    (tmp_path / "vp_model" / "hoja.py").write_text("import duckdb\n", encoding="utf-8")
    (tmp_path / "vp_model" / "medio.py").write_text(
        "def demo():\n    from vp_model import hoja\n    return hoja\n", encoding="utf-8"
    )
    raiz_falsa = tmp_path / "experiments"
    raiz_falsa.mkdir()
    ep = raiz_falsa / "e.py"
    ep.write_text("from vp_model.medio import demo\n", encoding="utf-8")
    cierre = ce._cierre_local(ep, tmp_path)
    assert (tmp_path / "vp_model" / "medio.py") in cierre
    assert (tmp_path / "vp_model" / "hoja.py") not in cierre, "siguió un import local diferido"


def test_el_universo_incluye_las_invocaciones_por_modulo() -> None:
    """★ Sin esto la comprobación NO cazaba `optuna`: el HPO entra por `-m vp_model.run_tuning`."""
    guion = (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8")
    assert ce.ANTE_MODULE.findall(guion), "no se reconocen las invocaciones `-m`"
    assert "vp_model.run_tuning" in ce.ANTE_MODULE.findall(guion)


def test_el_universo_sale_del_cierre_de_shells_de_la_campana() -> None:
    """Y no de todos los `.sh`: `run_statsforecast` sólo lo invoca el runbook histórico AQ, y
    hacer fallar la preparación por un guion que la campaña no ejecuta sería ruido."""
    import ast

    fuente = ast.parse((RAIZ / "tools" / "check_entrypoint_smoke.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(fuente) if isinstance(n, ast.FunctionDef) and n.name == "deferred_imports_resolve")
    texto = ast.get_source_segment((RAIZ / "tools" / "check_entrypoint_smoke.py").read_text(encoding="utf-8"), fn) or ""
    assert "run_rederivation.sh" in texto, "el universo no parte del runbook de la campaña"


# ═══════════════════════════════ 3 · el HPO deja de ser opcional
def test_las_tres_lineas_del_HPO_son_obligatorias() -> None:
    """Un paso que decide hiperparámetros no puede ser best-effort: o corre, o la campaña para."""
    vivas = [
        ln
        for ln in (RAIZ / "experiments" / "run_rederivation.sh").read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#") and "vp_model.run_tuning" in ln
    ]
    assert len(vivas) == 3, vivas
    for ln in vivas:
        assert ln.startswith("run_req "), f"sigue siendo best-effort: {ln}"


# ═══════════════════════════════ 4 · optuna, declarado donde toca
def test_optuna_esta_en_el_perfil_model_y_en_su_lock() -> None:
    py = (RAIZ / "pyproject.toml").read_text(encoding="utf-8")
    i = py.index("model = [")
    assert 'optuna==4.9.0"' in py[i : py.index("]", i)], "el perfil `model` no declara optuna"
    assert re.search(r"(?m)^optuna==4\.9\.0$", (RAIZ / "locks" / "model-cpu.txt").read_text(encoding="utf-8"))


def test_las_dos_versiones_de_optuna_coinciden() -> None:
    """El mismo paquete en dos intérpretes con versiones distintas es un estudio HPO ilegible."""
    base = re.search(r"(?m)^optuna==(\S+)$", (RAIZ / "locks" / "model-cpu.txt").read_text(encoding="utf-8"))
    deep = re.search(r"(?m)^optuna==(\S+?)\s", (RAIZ / "locks" / "deep-macos-arm64.txt").read_text(encoding="utf-8"))
    assert base and deep and base.group(1) == deep.group(1), (base, deep)


def test_el_lockset_vuelve_a_sellar_el_pyproject_real() -> None:
    """Tocar `pyproject.toml` invalida el manifiesto; regenerarlo es la mitad que lo repara."""
    import hashlib

    d = json.loads((RAIZ / "locks" / "lockset.json").read_text(encoding="utf-8"))
    real = "sha256:" + hashlib.sha256((RAIZ / "pyproject.toml").read_bytes()).hexdigest()
    assert d["sources"]["pyproject.toml"] == real
