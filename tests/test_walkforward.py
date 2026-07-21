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
    de la región de selección (que solo deben ver el pasado).

    F1: la corrupción se aplica a la serie F CRUDA, ANTES de transformar — la
    versión previa mutaba la serie YA rellenada, ciega por construcción a una fuga
    dentro de la propia transformación (la interpolación bidireccional usaba el
    bracket futuro del hueco y esta prueba jamás lo habría visto)."""
    import numpy as np

    from vp_model import models

    raw = dataset.load_series("mexico", "F3", "FAD").astype("float64")
    ts = models.to_timeseries(raw)
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

    raw_corrupt = raw.copy()
    raw_corrupt[raw_corrupt.index >= split] = 9.9e5  # basura en el futuro, PRE-transformación
    corrupt = models.to_timeseries(raw_corrupt)
    # B69: la basura en el futuro hace que SARIMA no converja y emita warnings de Statsmodels
    # (ConvergenceWarning + AR no-estacionario / MA no-invertible). Se CAPTURAN expresamente con
    # pytest.warns (documentando la intencion, sin filtro global de ignore) y se EXIGE que no aparezca
    # ninguna otra categoria — un warning distinto rompe la prueba.
    from statsmodels.tools.sm_exceptions import ConvergenceWarning

    with pytest.warns((ConvergenceWarning, UserWarning)) as rec:
        a, b = sel_fc(ts), sel_fc(corrupt)
    unexpected = [str(w.message) for w in rec if not issubclass(w.category, (ConvergenceWarning, UserWarning))]
    assert not unexpected, f"warning Statsmodels inesperado en la prueba de fuga: {unexpected}"
    assert np.allclose(a, b), "fuga temporal: el futuro alteró la selección"


def test_backtest_captures_fit_warnings_per_series(monkeypatch) -> None:
    """E5: los warnings de convergencia (statsmodels/SARIMA) se CAPTURAN y quedan
    registrados por serie en ``BacktestResult.warnings`` — no silenciados globalmente."""
    import warnings

    import numpy as np
    import pandas as pd
    from darts import TimeSeries
    from statsmodels.tools.sm_exceptions import ConvergenceWarning

    idx = pd.date_range("2010-01-01", periods=96, freq="MS")
    raw = pd.Series(np.arange(96, dtype="float64") * 30 + 1000, index=idx)
    monkeypatch.setattr(dataset, "load_series", lambda *a, **k: raw)

    class WarningModel:
        """Stub que emite el warning de convergencia en cada 'fold' (fit)."""

        def fit(self, series, **kwargs):
            return self

        def predict(self, n, **kwargs):
            raise NotImplementedError

        def historical_forecasts(self, series, *, start, **kwargs):
            n = len(series) - start
            for _ in range(n):  # un warning por origen, como statsmodels al reajustar
                warnings.warn("Maximum Likelihood optimization failed to converge.", ConvergenceWarning, stacklevel=2)
            return TimeSeries.from_times_and_values(series.time_index[start:], series.values()[start:])

    r = walkforward.backtest("sarima", "x", "F1", "FAD", model=WarningModel())
    assert len(r.warnings) == 1, r.warnings
    ((key, count),) = r.warnings.items()
    assert key.startswith("ConvergenceWarning:") and "converge" in key
    assert count == 96 - walkforward.MIN_TRAIN["FAD"]  # registrado por fold (un fit por origen)
    # y una corrida limpia registra dict vacío, no None
    clean = walkforward.backtest("naive1", "x", "F1", "FAD")
    assert clean.warnings == {}
