"""Salta la colección de los tests de la capa de modelado cuando el extra ``model`` no
está instalado.

El job base de CI (``lint-and-test``) instala solo ``.[dev]`` (sin darts/torch/statsmodels);
el job ``model-tests`` instala ``.[dev,model]`` y construye la BD. Sin este guard, pytest del
job base intenta colectar los tests de ``vp_model`` y muere con ``ModuleNotFoundError`` al
importar ``statsmodels``/``scipy``. Aquí, si falta una dependencia del extra, esos archivos
se omiten de la colección (en el job de modelado sí están y se ejecutan).
"""

import importlib.util

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
]

# `statsmodels` es del extra `model`; su ausencia marca el job base sin la capa de modelado.
if importlib.util.find_spec("statsmodels") is None:
    collect_ignore = _MODEL_TESTS
