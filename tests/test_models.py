"""Contrato del catálogo de modelos (US-D1).

Solo modelos rápidos (estadísticos + xgboost); las redes se validan en el motor de
walk-forward. Se omite si falta el extra de modelado (`pip install -e .[model]`) o
el almacén, para no romper CI que no instala darts/torch.
"""

from __future__ import annotations

import pytest

darts = pytest.importorskip("darts")  # noqa: F841

from vp_model import dataset, models  # noqa: E402

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")

FAST = ("naive", "arima", "sarima", "ets", "theta", "kalman", "rlinear", "xgboost")


def test_registry_matches_catalog() -> None:
    reg = models.registry()
    # 24 = 21 previos + naive1 + drift (AI1) + llt (AL4, promovido en AQ)
    assert len(reg) == len(models.MODEL_NAMES) == 24
    assert set(reg) == set(models.MODEL_NAMES)


def test_differenced_tree_extrapolates_trend() -> None:
    # El árbol-delta debe superar el máximo de train (el de nivel se satura: bug).
    from vp_model.feature_builder import FeatureBuilder

    raw = dataset.load_series("mexico", "F3", "FAD")
    ts = models.to_timeseries(raw)
    train = ts[:-24]
    # F1: los árboles llevan calendario + máscaras MNAR (la política vive en FeatureBuilder).
    cov = FeatureBuilder("xgboost").covariates(ts, raw)
    m = models.build_model("xgboost")  # Differenced(XGBModel)
    m.fit(train, future_covariates=cov)
    fc = m.predict(24, future_covariates=cov)
    assert float(fc.values().max()) > float(train.values().max()), "el árbol-delta debe extrapolar la tendencia"


def test_differenced_historical_forecasts_start_aligns_with_undifferenced() -> None:
    """FIX #21a: un `start` ENTERO (índice posicional de walkforward) debe caer en el
    MISMO origen calendario para un modelo Differenced que para uno sin diferenciar.
    series.diff() pierde la primera observación, así que antes del fix el backtest
    diferenciado arrancaba un mes tarde y evaluaba un origen menos que el pool
    estadístico (rompiendo la comparación justa entre los 24 modelos)."""
    import numpy as np
    import pandas as pd
    from darts import TimeSeries
    from darts.models import NaiveDrift, XGBModel

    idx = pd.date_range("2005-01-01", periods=90, freq="MS")
    vals = (np.cumsum(np.abs(np.sin(np.arange(90) / 6.0)) * 20 + 4) + 1000).astype("float64")
    series = TimeSeries.from_times_and_values(idx, vals)
    start = 60

    hf = dict(forecast_horizon=1, stride=1, retrain=True, last_points_only=True, verbose=False)
    undiff = NaiveDrift().historical_forecasts(series, start=start, **hf)
    diff = models.Differenced(XGBModel(lags=4)).historical_forecasts(series, start=start, **hf)

    # El primer origen evaluado debe ser el mes calendario en la posición dada...
    assert diff.time_index[0] == series.time_index[start]
    # ...idéntico al modelo sin diferenciar, con el mismo número de orígenes.
    assert diff.time_index[0] == undiff.time_index[0]
    assert len(diff) == len(undiff)

    # Guardia de leakage: perturbar el mes OBJETIVO no debe cambiar el pronóstico del
    # primer origen (solo puede usar datos <= t-1).
    vp = vals.copy()
    vp[start] += 1.0e5
    sp = TimeSeries.from_times_and_values(idx, vp)
    dp = models.Differenced(XGBModel(lags=4)).historical_forecasts(sp, start=start, **hf)
    assert abs(float(dp.values()[0, 0]) - float(diff.values()[0, 0])) < 1e-6


def test_to_timeseries_regular_no_gaps() -> None:
    ts = models.to_timeseries(dataset.load_series("china", "F1", "FAD"))
    assert ts.freq_str == "MS"
    assert ts.gaps().empty  # sin huecos: los modelos pueden entrenar


# B73/B82: warnings ESPERADOS por modelo, con categoría + prefijo de mensaje EXACTOS (no un substring
# genérico como "converge", que aceptaría warnings ajenos). En mexico/F3/FAD solo `arima` avisa (2
# UserWarning de Statsmodels: AR inicial no-estacionario + MA inicial no-invertible); el resto: cero.
_EXPECTED_WARNINGS: dict[str, list[tuple[type, str]]] = {
    "arima": [
        (UserWarning, "Non-stationary starting autoregressive parameters"),
        (UserWarning, "Non-invertible starting MA parameters"),
    ],
}


def _warnings_match_exactly(recorded, expected: list[tuple[type, str]]) -> str | None:
    """None si `recorded` casa EXACTAMENTE el multiset `expected` (categoría + prefijo de mensaje); si no, un
    mensaje del desajuste. Ni acepta warnings de más ni tolera ajenos por substring."""
    got = [(w.category, str(w.message)) for w in recorded]
    if len(got) != len(expected):
        return f"{len(got)} warnings, esperados {len(expected)}: {got}"
    remaining = list(expected)
    for cat, msg in got:
        m = next((e for e in remaining if issubclass(cat, e[0]) and msg.startswith(e[1])), None)
        if m is None:
            return f"warning inesperado: {cat.__name__}: {msg}"
        remaining.remove(m)
    return None if not remaining else f"faltan warnings esperados: {remaining}"


@pytest.mark.parametrize("name", FAST)
def test_fast_model_fits_and_forecasts(name: str) -> None:
    import warnings

    from vp_model.feature_builder import FeatureBuilder

    raw = dataset.load_series("mexico", "F3", "FAD")
    ts = models.to_timeseries(raw)
    model = models.build_model(name)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        if name == "xgboost":
            # F1: calendario (retardo 0) + máscaras MNAR (retardo −1) vía FeatureBuilder.
            cov = FeatureBuilder(name).covariates(ts, raw)
            model.fit(ts[:-12], future_covariates=cov)
            fc = model.predict(12, future_covariates=cov)
        else:
            model.fit(ts[:-12])
            fc = model.predict(12)
    mismatch = _warnings_match_exactly(rec, _EXPECTED_WARNINGS.get(name, []))
    assert mismatch is None, f"warnings de {name}: {mismatch}"
    assert len(fc) == 12
    import numpy as np

    assert np.isfinite(fc.values()).all()


def test_b82_unrelated_converge_warning_is_rejected() -> None:
    # un UserWarning ajeno que MENCIONE "converge" NO debe colarse (el substring genérico era el bug).
    import warnings

    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        warnings.warn("unrelated optimizer did not converge catastrophically", UserWarning, stacklevel=1)
    assert _warnings_match_exactly(rec, _EXPECTED_WARNINGS["arima"]) is not None  # NO casa el multiset esperado
    assert _warnings_match_exactly(rec, []) is not None  # tampoco "cero warnings"


def test_unknown_model_rejected() -> None:
    with pytest.raises(ValueError):
        models.build_model("nope")
