"""Contrato del motor walk-forward y las métricas (US-E1, US-F1, US-F2).

Solo modelos baratos; las redes se ejercitan en el barrido de comparación. Se omite
sin el extra de modelado o sin almacén.
"""

from __future__ import annotations

import pytest

pytest.importorskip("darts")

from vp_model import dataset, metrics, walkforward  # noqa: E402

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")


def test_metrics_perfect_forecast_is_zero() -> None:
    import numpy as np
    import pandas as pd
    from darts import TimeSeries

    y = pd.Series(np.arange(48, dtype="float64") * 30 + 1000, index=pd.date_range("2000-01-01", periods=48, freq="MS"))
    ts = TimeSeries.from_series(y)
    m = metrics.compute(ts[36:], ts[36:], ts[:36])
    assert m["mae"] == 0.0 and m["mase"] == 0.0


def test_pi_coverage_bounds() -> None:
    import pandas as pd
    from darts import TimeSeries

    a = TimeSeries.from_series(pd.Series([10.0, 20, 30], index=pd.date_range("2000-01-01", periods=3, freq="MS")))
    assert metrics.pi_coverage(a, a - 5, a + 5) == 1.0
    assert metrics.pi_coverage(a, a + 1, a + 2) == 0.0


@pytest.mark.parametrize("name", ["naive", "arima"])
def test_backtest_holdout_separated_and_leakage_free(name: str) -> None:
    r = walkforward.backtest(name, "mexico", "F3", "FAD")
    assert r.holdout["n"] == walkforward.HOLDOUT  # 24 meses reservados
    assert r.selection["n"] > 100  # región de selección amplia
    assert r.selection["mase"] > 0


def test_arima_beats_naive_on_mx_f3() -> None:
    # Comprobación de cordura: un modelo lineal debe mejorar al naïve estacional.
    naive = walkforward.backtest("naive", "mexico", "F3", "FAD")
    arima = walkforward.backtest("arima", "mexico", "F3", "FAD")
    assert arima.selection["mase"] < naive.selection["mase"]


def test_no_temporal_leakage_corrupt_future() -> None:
    """Prueba de oro de fuga: corromper el hold-out NO debe alterar los pronósticos
    de la región de selección (que solo deben ver el pasado)."""
    import numpy as np
    import pandas as pd

    from vp_model import models

    ts = models.to_timeseries(dataset.load_series("mexico", "F3", "FAD"))
    min_train = walkforward.MIN_TRAIN["FAD"]
    split = ts.time_index[-walkforward.HOLDOUT]

    def sel_fc(series):
        m = models.build_model("arima")
        fc = m.historical_forecasts(
            series,
            start=min_train,
            forecast_horizon=1,
            stride=1,  # type: ignore[attr-defined]
            retrain=True,
            last_points_only=True,
            verbose=False,
        )
        return fc.split_before(split)[0].values().flatten()

    vals = ts.values().flatten().copy()
    vals[-walkforward.HOLDOUT :] = 9.9e5  # basura en el futuro
    corrupt = models.TimeSeries.from_series(pd.Series(vals, index=ts.time_index.to_series()))
    assert np.allclose(sel_fc(ts), sel_fc(corrupt)), "fuga temporal: el futuro alteró la selección"
