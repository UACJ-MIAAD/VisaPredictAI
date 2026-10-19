"""G7 · desbloqueo — el perfil de CI decide si una ausencia se OMITE (dev) o FALLA (model).

El runner Linux destapó pruebas que pedían el extra `model` o un artefacto materializado (el parquet del panel) y que
el perfil base no provisiona. Omitirlas allí sólo es honesto si el perfil `model` las corre y FALLA cuando le falta
algo: si no, una prueba obligatoria desaparecería en silencio de los dos jobs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import conftest as cf

RAIZ = Path(__file__).resolve().parents[1]


def test_en_el_perfil_base_una_ausencia_se_omite_declarada(monkeypatch) -> None:
    monkeypatch.setattr(cf, "PERFIL", "dev")
    with pytest.raises(pytest.skip.Exception, match="model-tests"):
        cf.requiere(modulos=("modulo_que_no_existe_g7",))
    with pytest.raises(pytest.skip.Exception, match="model-tests"):
        cf.requiere(artefactos=("data/processed/no_existe_g7.parquet",))


def test_en_el_perfil_model_una_ausencia_falla(monkeypatch) -> None:
    monkeypatch.setattr(cf, "PERFIL", "model")
    with pytest.raises(pytest.fail.Exception, match="debe provisionar"):
        cf.requiere(modulos=("modulo_que_no_existe_g7",))
    with pytest.raises(pytest.fail.Exception, match="debe provisionar"):
        cf.requiere(artefactos=("data/processed/no_existe_g7.parquet",))


def test_control_lo_que_existe_no_se_omite_en_ningun_perfil(monkeypatch) -> None:
    for perfil in ("", "dev", "model"):
        monkeypatch.setattr(cf, "PERFIL", perfil)
        cf.requiere(modulos=("json",), artefactos=("pyproject.toml",))


def test_el_extra_del_perfil_model_se_deriva_de_su_lock() -> None:
    extra = cf.extra_model()
    assert {"scipy", "darts", "statsmodels", "lightgbm", "xgboost", "torch", "optuna"} <= extra
    assert "neuralforecast" not in extra, "neuralforecast es del perfil deep, no del model"
    assert extra <= cf.PESADOS


def test_el_perfil_model_aborta_si_le_falta_un_pesado(monkeypatch) -> None:
    monkeypatch.setattr(cf, "PERFIL", "model")
    real = cf.importlib.util.find_spec
    monkeypatch.setattr(cf.importlib.util, "find_spec", lambda n, *a: None if n == "darts" else real(n, *a))
    with pytest.raises(pytest.exit.Exception, match="darts"):
        cf.pytest_sessionstart(None)


def test_control_el_perfil_model_completo_arranca(monkeypatch) -> None:
    monkeypatch.setattr(cf, "PERFIL", "model")
    monkeypatch.setattr(cf.importlib.util, "find_spec", lambda n, *a: object())
    cf.pytest_sessionstart(None)


def test_un_perfil_desconocido_aborta_la_sesion() -> None:
    entorno = {**os.environ, "VP_CI_PROFILE": "modelo", "PYTHONPATH": str(RAIZ)}
    fin = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "--collect-only",
         "tests/test_perfil_ci_g7.py"],
        cwd=RAIZ, env=entorno, capture_output=True, text=True,
    )  # fmt: skip
    assert fin.returncode == 4 and "no es un perfil conocido" in fin.stdout + fin.stderr, fin.stdout[-500:]


def test_el_workflow_declara_el_perfil_de_cada_job() -> None:
    """El perfil vive en el workflow (no en una variable del operador): `dev` en el job base y `model` en el de
    modelado, que es el que construye panel y BD y corre la suite completa en su paso de cobertura amplia."""
    import yaml

    jobs = yaml.safe_load((RAIZ / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    assert jobs["lint-and-test"]["env"]["VP_CI_PROFILE"] == "dev"
    assert jobs["model-tests"]["env"]["VP_CI_PROFILE"] == "model"
    pasos = " ".join(str(p.get("run", "")) for p in jobs["model-tests"]["steps"])
    assert "pipeline.build_database" in pasos and "--cov=vp_model" in pasos
