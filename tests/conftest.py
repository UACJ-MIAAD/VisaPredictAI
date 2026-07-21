"""Salta la colección de los tests de la capa de modelado cuando el extra ``model`` no
está instalado.

El job base de CI (``lint-and-test``) instala solo ``.[dev]`` (sin darts/torch/statsmodels);
el job ``model-tests`` instala ``.[dev,model]`` y construye la BD. Sin este guard, pytest del
job base intenta colectar los tests de ``vp_model`` y muere con ``ModuleNotFoundError`` al
importar ``statsmodels``/``scipy``. Aquí, si falta una dependencia del extra, esos archivos
se omiten de la colección (en el job de modelado sí están y se ejecutan).
"""

import importlib.util

# Fase 4 (P0R.5): warnings como CONTRATO. `error` = todo warning es fallo, SIN supresión global (prohibido
# `ignore::Warning`); las únicas excepciones son 4 warnings upstream/experimentales INEVITABLES (sklearn/optuna/scipy
# desde vp_model/tune.py), cada uno por un filtro ESTRECHO (message-prefix + categoría) documentado con expiry en
# security/warnings_registry.json. La biyección registro⇔estos filtros la valida tools/check_warnings.py. Va en el
# conftest (NO en pyproject) para mantener pyproject.toml BYTE-IDÉNTICO y no tocar el manifiesto de locks (lockset.json
# pinnea el hash de pyproject). Aplica a toda la sesión vía `pytest_configure`.
FILTERWARNINGS = [
    "error",
    "ignore:X does not have valid feature names, but LGBMRegressor was fitted with feature names:UserWarning",
    "ignore:Argument ``multivariate`` is an experimental feature:optuna.exceptions.ExperimentalWarning",
    "ignore:Argument ``group`` is an experimental feature:optuna.exceptions.ExperimentalWarning",
    "ignore:An input array is constant:scipy.stats.ConstantInputWarning",
]


def pytest_configure(config):
    for _f in FILTERWARNINGS:
        config.addinivalue_line("filterwarnings", _f)


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
