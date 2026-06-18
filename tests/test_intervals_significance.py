"""Contrato de intervalos de predicción (US-E3) y significancia (US-F3).

``significance`` solo usa numpy/scipy -> siempre corre. ``intervals`` usa darts ->
se omite si falta el extra de modelado.
"""

from __future__ import annotations

import numpy as np
import pytest

from vp_model import significance as sig


def test_dm_detects_worse_model() -> None:
    rng = np.random.default_rng(1)
    good = rng.normal(0, 1, 200)
    bad = rng.normal(0, 3, 200)
    stat, p = sig.dm_test(good, bad)
    assert stat < 0 and p < 0.01


def test_giacomini_white_detects_difference() -> None:
    rng = np.random.default_rng(3)
    _, p = sig.giacomini_white(rng.normal(0, 1, 200), rng.normal(0, 3, 200))
    assert p < 0.05


def test_model_confidence_set_excludes_worst() -> None:
    rng = np.random.default_rng(4)
    losses = {"good": rng.normal(0.2, 0.05, 150) ** 2, "bad": rng.normal(1.0, 0.05, 150) ** 2}
    mcs = sig.model_confidence_set(losses, reps=200)
    assert "bad" not in mcs


def test_friedman_nemenyi_ranks() -> None:
    import pandas as pd

    rng = np.random.default_rng(5)
    sc = pd.DataFrame(
        {
            "bueno": rng.normal(0.2, 0.05, 30),
            "medio": rng.normal(0.5, 0.05, 30),
            "malo": rng.normal(0.9, 0.05, 30),
        }
    )
    fn = sig.friedman_nemenyi(sc)
    assert fn["friedman_p"] < 0.05
    assert fn["avg_rank"].index[0] == "bueno"


def test_probabilistic_metrics() -> None:
    pytest.importorskip("darts")
    import pandas as pd
    from darts import TimeSeries

    from vp_model import metrics

    idx = pd.date_range("2000-01-01", periods=24, freq="MS")
    actual = TimeSeries.from_series(pd.Series(np.arange(24, dtype="float64") * 10, index=idx))
    # interval score: no cubrir (a igual amplitud) penaliza más que cubrir.
    assert metrics.interval_score(actual, actual + 10, actual + 20) > metrics.interval_score(
        actual, actual - 5, actual + 5
    )
    assert metrics.msis(actual, actual - 5, actual + 5, actual) > 0
    # CRPS de muestras exactas ~ 0.
    s = np.repeat(actual.values(), 100, axis=1)[:, None, :]
    exact = TimeSeries.from_times_and_values(actual.time_index, s)
    assert metrics.crps(actual, exact) < 1e-6
    # pinball en la mediana = 0.5 * |error|.
    assert abs(metrics.pinball(actual, actual + 10, 0.5) - 5.0) < 1e-6


def test_dm_no_difference() -> None:
    rng = np.random.default_rng(2)
    e = rng.normal(0, 1, 200)
    _, p = sig.dm_test(e, e + 1e-9)
    assert p > 0.05


def test_dm_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        sig.dm_test(np.zeros(10), np.zeros(11))


def test_holm_monotone_and_controls() -> None:
    raw = {"a": 0.01, "b": 0.04, "c": 0.04}
    adj = sig.holm(raw)
    assert adj["a"][1] is True  # 0.01*3 = 0.03 < 0.05
    for k, v in raw.items():
        assert adj[k][0] >= v  # el ajuste nunca reduce el p-valor


def test_conformal_coverage_near_nominal() -> None:
    pytest.importorskip("darts")
    import pandas as pd
    from darts import TimeSeries

    from vp_model import intervals
    from vp_model.metrics import pi_coverage

    rng = np.random.default_rng(0)
    idx = pd.date_range("2000-01-01", periods=300, freq="MS")
    truth = np.arange(300, dtype="float64") * 10
    actual = TimeSeries.from_series(pd.Series(truth + rng.normal(0, 50, 300), index=idx))
    pred = TimeSeries.from_series(pd.Series(truth, index=idx))
    iv = intervals.conformal(pred[200:], actual[:200], pred[:200])
    assert 0.88 <= pi_coverage(actual[200:], iv.lower, iv.upper) <= 1.0
