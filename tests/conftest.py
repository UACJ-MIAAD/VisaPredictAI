"""Salta la colección de los tests de la capa de modelado cuando el extra ``model`` no
está instalado.

El job base de CI (``lint-and-test``) instala solo ``.[dev]`` (sin darts/torch/statsmodels);
el job ``model-tests`` instala ``.[dev,model]`` y construye la BD. Sin este guard, pytest del
job base intenta colectar los tests de ``vp_model`` y muere con ``ModuleNotFoundError`` al
importar ``statsmodels``/``scipy``. Aquí, si falta una dependencia del extra, esos archivos
se omiten de la colección (en el job de modelado sí están y se ejecutan).
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# D2-C: los warnings de la suite son un CONTRATO. `error` global = cualquier warning nuevo hace
# fallar su test, SIN supresión global (prohibido `ignore::Warning`). Las únicas excepciones son
# las del registro positivo `security/warnings_registry.json`, todas upstream y con filtro
# ESTRECHO (prefijo de mensaje + categoría) y fecha de caducidad. La biyección registro⇔esta lista
# la valida `tools/check_warnings.py` (job `consistency` de CI). Vive AQUÍ y no en pyproject.toml
# para dejar ese archivo BYTE-IDÉNTICO: `locks/lockset.json` pinnea su hash.
FILTERWARNINGS = [
    "error",
    "ignore:X\\ does\\ not\\ have\\ valid\\ feature\\ names,\\ but\\ LGBMRegressor\\ was\\ fitted\\ with\\ feature\\ names:UserWarning",
    "ignore:Argument\\ ``multivariate``\\ is\\ an\\ experimental\\ feature:optuna.exceptions.ExperimentalWarning",
    "ignore:Argument\\ ``group``\\ is\\ an\\ experimental\\ feature:optuna.exceptions.ExperimentalWarning",
    "ignore:An\\ input\\ array\\ is\\ constant:scipy.stats.ConstantInputWarning",
    "ignore:Non\\-stationary\\ starting\\ autoregressive\\ parameters\\ found\\.\\ Using\\ zeros\\ as\\ starting\\ parameters\\.:UserWarning",
    "ignore:Non\\-invertible\\ starting\\ MA\\ parameters\\ found\\.\\ Using\\ zeros\\ as\\ starting\\ parameters\\.:UserWarning",
    "ignore:Maximum\\ Likelihood\\ optimization\\ failed\\ to\\ converge\\.\\ Check\\ mle_retvals:statsmodels.tools.sm_exceptions.ConvergenceWarning",
    "ignore:Optimization\\ failed\\ to\\ converge\\.\\ Check\\ mle_retvals\\.:statsmodels.tools.sm_exceptions.ConvergenceWarning",
]


def _filter_category_available(filt: str) -> bool:
    """Un filtro con categoría PUNTEADA de terceros (optuna/scipy) solo se aplica si su módulo raíz
    está instalado en este job: el job base (`.[dev]`) no los trae y pytest crashea al RESOLVER la
    categoría (con `error`, ese PytestConfigWarning se vuelve INTERNALERROR). Esos warnings solo se
    emiten con el módulo presente, así que saltarlos es seguro. `find_spec` no importa el módulo."""
    parts = filt.split(":")
    if len(parts) < 3 or "." not in parts[-1]:
        return True
    return importlib.util.find_spec(parts[-1].split(".")[0]) is not None


def pytest_configure(config):
    for _filt in FILTERWARNINGS:
        if _filter_category_available(_filt):
            config.addinivalue_line("filterwarnings", _filt)


_MODEL_TESTS = [
    "test_dataset.py",
    "test_eda_preprocess.py",
    "test_models.py",
    "test_walkforward.py",
    "test_intervals_significance.py",
    "test_config_report.py",
    "test_features.py",
    "test_missingness.py",
    "test_feature_select.py",
    "test_ensemble.py",
    "test_ens_brutal.py",  # ensembles épica AM → vp_model + darts/scipy/xgboost
    "test_forecast_scoring.py",  # importa score_forecasts → vp_model.metrics → darts
    "test_model_regression.py",  # golden-master del walk-forward → vp_model + darts
    "test_temporal_leakage.py",  # metamórficos de fuga temporal (US-F1) → vp_model + darts
    "test_metric_regression.py",  # protocolo de métricas en fixtures sintéticas (US-E5) → vp_model + darts
    "test_champion.py",  # harness campeón-retador → vp_model + scipy
    "test_pi_brutal.py",  # intervalos (épica AN) → vp_model.intervals + darts/scipy
    "test_tune_brutal.py",  # HPO (épica AK) → vp_model.tune + darts/optuna
]

# `statsmodels` es del extra `model`; su ausencia marca el job base sin la capa de modelado.
if importlib.util.find_spec("statsmodels") is None:
    collect_ignore = _MODEL_TESTS


# ---------------------------------------------------------------------------
# Andamiaje compartido para las pruebas que corren el guardián de verdad sobre una COPIA
# del repositorio. Vive aquí y no en un archivo de pruebas porque ya lo usan dos, y una
# copia divergente es justo como se coló el defecto del checkout anidado de M66.
# ---------------------------------------------------------------------------
RAIZ = Path(__file__).resolve().parents[1]
DELIVERABLE = "reports/latex/ProyectoI_VisaPredictAI.tex"

_latex_raw = os.environ.get("VP_LATEX_DIR")
LATEX_ROOT = (
    ((RAIZ / _latex_raw) if _latex_raw and not Path(_latex_raw).is_absolute() else Path(_latex_raw))
    if _latex_raw
    else RAIZ.parent / "VisaPredictAI_LaTeX"
)
LATEX_ROOT = LATEX_ROOT.resolve()


def nombre_si_cuelga_del_repo(destino: Path) -> str | None:
    """Nombre de primer nivel de `destino` si vive DENTRO de `RAIZ`; `None` si es hermano.

    En local el repositorio LaTeX es hermano del de datos; en CI se hace checkout en
    `RAIZ/latex_repo` (`VP_LATEX_DIR`). Se deriva del valor efectivo en vez de teclear el
    nombre: si el workflow cambia la ruta del checkout, esto la sigue.
    """
    try:
        partes = destino.relative_to(RAIZ).parts
    except ValueError:
        return None
    return partes[0] if partes else None


def ignore_para(latex_root: Path):
    """Qué se deja fuera de la copia del repositorio de datos.

    Cuando el repositorio LaTeX cuelga del de datos hay que excluirlo de la PRIMERA copia: la
    SEGUNDA lo copia a propósito a `work/latex_repo` y, si ya estaba, `copytree` revienta con
    `FileExistsError`. En local no pasaba porque allí el repositorio es hermano.
    """
    anidado = nombre_si_cuelga_del_repo(latex_root)
    return shutil.ignore_patterns(
        ".git",
        "ante",
        "ante_nf",
        ".vp_envs",
        "data",
        "models",
        "mlartifacts",
        "mlruns_staging",
        "node_modules",
        ".dvc",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        *((anidado,) if anidado else ()),
    )


@pytest.fixture(scope="session")
def repo_guardian(tmp_path_factory) -> Path:
    """Copia del repositorio de datos con el documental montado en `latex_repo`."""
    work = tmp_path_factory.mktemp("guard") / "repo"
    shutil.copytree(RAIZ, work, symlinks=True, ignore=ignore_para(LATEX_ROOT))
    shutil.copytree(
        LATEX_ROOT,
        work / "latex_repo",
        ignore=shutil.ignore_patterns(".git", "*.pdf", "*.log", "*.aux", "*.out", "*.toc", "*.lof", "*.lot"),
    )
    return work


def sembrar_y_correr(repo: Path, frase: str) -> subprocess.CompletedProcess:
    """Añade `frase` al entregable de la copia, corre el guardián y restaura el archivo."""
    target = repo / "latex_repo" / DELIVERABLE
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(original + f"\n\n{frase}\n", encoding="utf-8")
        env = {**os.environ, "VP_LATEX_DIR": "latex_repo"}
        return subprocess.run(
            [sys.executable, "tools/check_consistency.py"], cwd=repo, env=env, capture_output=True, text=True
        )
    finally:
        target.write_text(original, encoding="utf-8")
