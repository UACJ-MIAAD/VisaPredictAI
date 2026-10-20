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


@pytest.fixture
def vinculo_fuera_de_esta_prueba(monkeypatch):
    """G7 · B5a: sustituye SÓLO la verificación del vínculo del recibo /3 (reconciliación + candidato reales).

    Para pruebas de puertas ortogonales a ese vínculo. El vínculo se prueba de punta a punta, sin sustitutos, en
    `tests/test_g7_publicacion_reconciliada.py`. Un subproceso NO hereda esta sustitución: allí se valida en proceso.
    """
    from tests import campana_valida
    from vp_model import vinculo_publicacion as vp

    monkeypatch.setattr(vp, "problema", lambda *_a, **_k: None)
    monkeypatch.setattr(vp, "arbol_de", lambda *_a, **_k: campana_valida.ARBOL_LIGERO)


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
    "ignore:The\\ 'generic'\\ unit\\ for\\ NumPy\\ timedelta\\ is\\ deprecated:DeprecationWarning",
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


#: Marcadores propios. Se registran AQUÍ y no en `pyproject.toml`, que `locks/lockset.json`
#: pinnea por sha256: bajo el contrato `error`, un marcador sin registrar es un fallo de colección.
MARKERS = ("slow: barrido completo sobre el catálogo real; sólo corre con VP_FULL_SWEEP=1",)


def pytest_configure(config):
    for _filt in FILTERWARNINGS:
        if _filter_category_available(_filt):
            config.addinivalue_line("filterwarnings", _filt)
    for _marker in MARKERS:
        config.addinivalue_line("markers", _marker)


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
    # G7 · desbloqueo: dos módulos de modelado enteros que el runner Linux no podía recolectar
    "test_exclusiones_acreditadas_m74e_r20.py",  # vp_model.persist_forecasts → vp_model.models → xgboost
    "test_m74f_complemento.py",  # score_e3_campaign → vp_model.metrics → darts
]

# `statsmodels` es del extra `model`; su ausencia marca el job base sin la capa de modelado.
if importlib.util.find_spec("statsmodels") is None:
    collect_ignore = _MODEL_TESTS

# ---------------------------------------------------------------------------
# G7 · desbloqueo: el PERFIL de CI que corre la suite. `model-tests` fija `VP_CI_PROFILE=model`: instala el extra y
# materializa panel y BD, así que ahí la falta de una dependencia pesada o de un artefacto es un FALLO. En el perfil
# base (`lint-and-test`, sin variable o `dev`) es una omisión DECLARADA que el job `model` cubre: la misma prueba corre
# allí en el paso de cobertura amplia, que ejecuta la suite completa.
# ---------------------------------------------------------------------------
PERFILES = ("", "dev", "model")
PERFIL = os.environ.get("VP_CI_PROFILE", "")
#: Lo que el job base NO instala (`.[dev]` sin el extra `model`). Única lista: la lee también `test_base_job_imports`.
PESADOS = frozenset({"scipy", "darts", "statsmodels", "lightgbm", "xgboost", "torch", "optuna", "neuralforecast"})
_RAIZ_REPO = Path(__file__).resolve().parents[1]
#: El lock del perfil `model` en el runner: de él se DERIVA qué pesados debe traer ese perfil (neuralforecast es deep).
LOCK_MODEL = _RAIZ_REPO / "locks" / "model-cpu-linux-x86_64.txt"


def extra_model() -> frozenset[str]:
    """Los módulos pesados que el perfil `model` instala, según su lock (no según una lista escrita a mano)."""
    fijados = {
        linea.split("==")[0].strip().lower()
        for linea in LOCK_MODEL.read_text(encoding="utf-8").splitlines()
        if "==" in linea and not linea.lstrip().startswith("#")
    }
    return frozenset(m for m in PESADOS if m in fijados)


def requiere(*, modulos: tuple[str, ...] = (), artefactos: tuple[str, ...] = ()) -> None:
    """Omite si este perfil no provisiona lo pedido; en el perfil `model`, su ausencia FALLA.

    ``artefactos`` son rutas relativas a la raíz del repositorio que el job `model` materializa (p. ej., el parquet
    que escribe `pipeline.build_database`): verificar la procedencia REAL exige el archivo real, nunca uno sustituto.
    """
    faltan = [m for m in modulos if importlib.util.find_spec(m) is None]
    faltan += [a for a in artefactos if not (_RAIZ_REPO / a).is_file()]
    if not faltan:
        return
    if PERFIL == "model":
        pytest.fail(f"el perfil `model` debe provisionar {faltan}", pytrace=False)
    pytest.skip(f"requiere {faltan}: lo provisiona el job `model-tests` (VP_CI_PROFILE=model)")


def pytest_sessionstart(session):
    """El perfil se declara bien y, si es `model`, trae TODO su extra: ninguna prueba se omite por un pesado ausente."""
    if PERFIL not in PERFILES:
        pytest.exit(f"VP_CI_PROFILE={PERFIL!r} no es un perfil conocido {PERFILES}", returncode=4)
    if PERFIL == "model":
        faltan = sorted(m for m in extra_model() if importlib.util.find_spec(m) is None)
        if faltan:
            pytest.exit(f"el perfil `model` no trae {faltan}: sus pruebas se omitirían en silencio", returncode=4)


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


@pytest.fixture
def panel_semillas(tmp_path, monkeypatch):
    """M74-E-R7 · panel largo sintético servido como el CSV canónico: 25 series FAD × 96 meses F.

    Devuelve el NIVEL ``(unique_id, ds, y)`` construido a mano, sin `load_panel`: la rejilla que el
    lector deriva del panel se contrasta así con una construida por otro camino.
    """
    import pandas as pd

    import vp_data.config as data_config

    meses = pd.date_range("2016-01-01", periods=96, freq="MS")
    paises = ("mexico", "india", "china", "philippines", "all_chargeability")
    series = [(p, c) for p in paises for c in ("F1", "F2A", "F2B", "F3", "F4")]
    filas = [
        {
            "country": p,
            "block": "family",
            "category": c,
            "table": "FAD",
            "bulletin_date": d.date().isoformat(),
            "status": "F",
            "days_since_base": float(9000 + 31 * i + 7 * j),
        }
        for j, (p, c) in enumerate(series)
        for i, d in enumerate(meses)
    ]
    ruta = tmp_path / "visa_panel_long.csv"
    pd.DataFrame(filas).to_csv(ruta, index=False)
    monkeypatch.setattr(data_config, "PANEL_PATH", ruta)
    return pd.DataFrame(
        {
            "unique_id": [f"{f['country']}/family/{f['category']}" for f in filas],
            "ds": pd.to_datetime([f["bulletin_date"] for f in filas]),
            "y": [f["days_since_base"] for f in filas],
        }
    )


@pytest.fixture
def campana_sellada():
    """M74-E-R9/R10 · fábrica de manifiesto + sello LEGÍTIMOS bajo ``root``.

    ★ R10: el sello es el que emitió el preflight real de `490befb` (``tests/data/sello_campana_legitimo.json``)
    con la identidad sustituida; el «mínimo válido» de R9 consagraba un bypass. ``sello`` admite un dict o bytes
    crudos para construir ataques, y ``fabricar.sello`` devuelve una copia legítima para una identidad.
    """
    import copy
    import hashlib
    import json

    legitimo = json.loads((RAIZ / "tests" / "data" / "sello_campana_legitimo.json").read_text(encoding="utf-8"))

    def sello_legitimo(head: str = "a" * 40, dirty: bool = False) -> dict:
        sello = copy.deepcopy(legitimo)
        sello["git"] = {"head": head, "dirty": dirty}
        return sello

    def fabricar(root, *, campaign_id=None, head="a" * 40, dirty=False, sello=None):
        root = Path(root)
        cid = campaign_id or f"rederiv_{head[:7]}_20260914T000000"
        ruta = f"reports/logs/preflight_{cid}.json"
        contenido = sello_legitimo(head, dirty) if sello is None else sello
        crudo = contenido if isinstance(contenido, bytes) else json.dumps(contenido).encode("utf-8")
        (root / ruta).parent.mkdir(parents=True, exist_ok=True)
        (root / ruta).write_bytes(crudo)
        manifiesto = root / "reports" / "campaign" / "campaign_manifest.json"
        manifiesto.parent.mkdir(parents=True, exist_ok=True)
        datos = {"campaign_id": cid, "sha": head, "git_sha": head, "dirty": dirty}
        datos |= {"started_at": "2000-01-01T00:00:00+00:00", "preflight": ruta}
        datos["preflight_sha256"] = hashlib.sha256(crudo).hexdigest()
        manifiesto.write_text(json.dumps(datos), encoding="utf-8")
        return manifiesto

    def identidad(manifiesto) -> dict:
        """★ M74-E-R13 · los kwargs de `open_campaign` que describe un manifiesto fabricado: publicar ya lo exige."""
        datos = json.loads(Path(manifiesto).read_text(encoding="utf-8"))
        return {"campaign_id": datos["campaign_id"], "source_git_sha": datos["git_sha"],
                "git_dirty": datos["dirty"], "input_seal_sha256": datos["preflight_sha256"]}  # fmt: skip

    fabricar.sello = sello_legitimo  # type: ignore[attr-defined]
    fabricar.identidad = identidad  # type: ignore[attr-defined]
    return fabricar
