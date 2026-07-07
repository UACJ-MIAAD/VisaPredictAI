"""Contrato del campeón por horizonte (``vp_model.horizon``): walk-forward
multi-horizonte con orígenes RODANTES, puntuación F-only, escala MASE canónica y
sin fuga temporal. Omite sin ``darts`` o sin el almacén DuckDB.
"""

from __future__ import annotations

import pytest

pytest.importorskip("darts")

from vp_model import dataset, horizon, metrics  # noqa: E402
from vp_model.config import HOLDOUT, HORIZONS, MIN_TRAIN  # noqa: E402

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")


def test_mase_grows_with_horizon() -> None:
    """El error se ACUMULA con el horizonte (firma de pronóstico multi-paso real)."""
    mh = horizon.mase_by_horizon("naive1", "mexico", "F3", "FAD", max(HORIZONS))
    assert mh[1] > 0
    assert mh[max(mh)] > mh[1]


def test_forecasts_rolling_target_dates() -> None:
    """Todos los horizontes presentes; el objetivo de h=6 va 5 meses tras el de h=1
    para el MISMO primer origen (offset de horizonte correcto)."""
    fc = horizon.forecasts_by_horizon("naive1", "mexico", "F3", "FAD", max(HORIZONS))
    assert set(fc) >= set(HORIZONS)
    o1 = fc[1].index.min().to_period("M")
    o6 = fc[6].index.min().to_period("M")
    assert (o6 - o1).n == 5  # mismo primer origen, objetivo de h=6 va 5 meses después


def test_scale_is_canonical() -> None:
    """El MASE por horizonte usa EXACTAMENTE la escala canónica ``naive_scale_before``."""
    import numpy as np

    from vp_model.feature_builder import FeatureBuilder

    raw = dataset.load_series("mexico", "F3", "FAD").astype("float64")
    ts = FeatureBuilder("naive1").to_timeseries(dataset.load_series("mexico", "F3", "FAD"))
    scale = metrics.naive_scale_before(raw, ts.time_index[-HOLDOUT])
    fc = horizon.forecasts_by_horizon("naive1", "mexico", "F3", "FAD", 1)[1]
    common = [d for d in fc.index if d in set(raw.index)]
    mae = float(np.mean(np.abs(raw.loc[common].to_numpy() - fc.loc[common].to_numpy())))
    mh = horizon.mase_by_horizon("naive1", "mexico", "F3", "FAD", 1)
    assert abs(mh[1] - mae / scale) < 1e-9


def test_multi_horizon_no_leakage() -> None:
    """Prueba de oro: corromper el futuro NO altera el pronóstico multi-horizonte de un
    origen temprano (que solo debe ver el pasado)."""
    import numpy as np
    import pandas as pd

    from vp_model import models

    ts = models.to_timeseries(dataset.load_series("mexico", "F3", "FAD"))
    min_train = MIN_TRAIN["FAD"]

    def early_forecast(series):
        m = models.build_model("theta")
        per = m.historical_forecasts(
            series,
            start=min_train,
            forecast_horizon=6,
            stride=1,  # type: ignore[attr-defined]
            retrain=True,
            last_points_only=False,
            verbose=False,
        )
        return np.asarray(per[0].values()).ravel()  # el primer (más temprano) origen

    vals = ts.values().flatten().copy()
    vals[-HOLDOUT:] = 9.9e5  # basura en el futuro
    corrupt = models.TimeSeries.from_series(pd.Series(vals, index=ts.time_index.to_series()))
    assert np.allclose(early_forecast(ts), early_forecast(corrupt)), "fuga: el futuro alteró un origen temprano"
