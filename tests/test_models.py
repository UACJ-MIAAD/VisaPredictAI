"""Contrato del catálogo de modelos (US-D1).

Solo modelos rápidos (estadísticos + xgboost); las redes se validan en el motor de
walk-forward. Se omite si falta el extra de modelado (`pip install -e .[model]`) o
el almacén, para no romper CI que no instala darts/torch.
"""

from __future__ import annotations

import pytest

darts = pytest.importorskip("darts")  # noqa: F841

from vp_model import dataset, models, preprocess  # noqa: E402

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")

FAST = ("naive", "arima", "sarima", "ets", "theta", "kalman", "rlinear", "xgboost")


def test_registry_matches_catalog() -> None:
    reg = models.registry()
    # 24 = 21 previos + naive1 + drift (AI1) + llt (AL4, promovido en AQ)
    assert len(reg) == len(models.MODEL_NAMES) == 24
    assert set(reg) == set(models.MODEL_NAMES)


def test_differenced_tree_extrapolates_trend() -> None:
    # El árbol-delta debe superar el máximo de train (el de nivel se satura: bug).
    from darts import TimeSeries

    ts = models.to_timeseries(dataset.load_series("mexico", "F3", "FAD"))
    train = ts[:-24]
    cov = TimeSeries.from_dataframe(preprocess.calendar_features(ts.time_index))
    m = models.build_model("xgboost")  # Differenced(XGBModel)
    m.fit(train, future_covariates=cov)
    fc = m.predict(24, future_covariates=cov)
    assert float(fc.values().max()) > float(train.values().max()), "el árbol-delta debe extrapolar la tendencia"


def test_to_timeseries_regular_no_gaps() -> None:
    ts = models.to_timeseries(dataset.load_series("china", "F1", "FAD"))
    assert ts.freq_str == "MS"
    assert ts.gaps().empty  # sin huecos: los modelos pueden entrenar


@pytest.mark.parametrize("name", FAST)
def test_fast_model_fits_and_forecasts(name: str) -> None:
    from darts import TimeSeries

    ts = models.to_timeseries(dataset.load_series("mexico", "F3", "FAD"))
    model = models.build_model(name)
    if name == "xgboost":
        cov = TimeSeries.from_dataframe(preprocess.calendar_features(ts.time_index))
        model.fit(ts[:-12], future_covariates=cov)
        fc = model.predict(12, future_covariates=cov)
    else:
        model.fit(ts[:-12])
        fc = model.predict(12)
    assert len(fc) == 12
    import numpy as np

    assert np.isfinite(fc.values()).all()


def test_unknown_model_rejected() -> None:
    with pytest.raises(ValueError):
        models.build_model("nope")
