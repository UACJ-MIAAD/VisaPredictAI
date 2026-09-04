"""Salta la colección de los tests de la capa de modelado cuando el extra ``model`` no
está instalado.

El job base de CI (``lint-and-test``) instala solo ``.[dev]`` (sin darts/torch/statsmodels);
el job ``model-tests`` instala ``.[dev,model]`` y construye la BD. Sin este guard, pytest del
job base intenta colectar los tests de ``vp_model`` y muere con ``ModuleNotFoundError`` al
importar ``statsmodels``/``scipy``. Aquí, si falta una dependencia del extra, esos archivos
se omiten de la colección (en el job de modelado sí están y se ejecutan).
"""

import importlib.util

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
