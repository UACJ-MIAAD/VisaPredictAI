"""Lógica pura de config, procedencia, ranking y generación de tabla (C3, A3).

Sube la cobertura de la orquestación que antes estaba a 0%. ``report`` y
``run_comparison`` importan darts indirectamente -> se omiten sin el extra.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from vp_model import config


def test_run_metadata_has_provenance_keys() -> None:
    m = config.run_metadata()
    for k in ("run_id", "timestamp", "git_sha", "git_dirty", "python", "seed", "libs", "walkforward", "hyperparams"):
        assert k in m, k
    assert m["seed"] == config.RANDOM_SEED
    assert m["walkforward"]["holdout"] == config.HOLDOUT
    assert "arima" in m["hyperparams"]
    assert m["run_id"]  # no vacío


def test_seed_everything_is_deterministic() -> None:
    config.seed_everything(123)
    a = [random.random() for _ in range(5)]
    config.seed_everything(123)
    b = [random.random() for _ in range(5)]
    assert a == b


def test_single_source_seasonal_constant() -> None:
    # El duplicado SEASONAL_M/SEASONAL_PERIOD quedó eliminado: ambos apuntan a config.
    from vp_model import metrics, models

    assert metrics.SEASONAL_M == config.SEASONAL_PERIOD == models.SEASONAL_PERIOD == 12


def _fake_results() -> pd.DataFrame:
    rows = []
    for cat in ("F1", "F2A"):
        for model, mase in [("sarima", 0.10), ("arima", 0.20), ("naive", 1.0)]:
            rows.append(
                {
                    "run_id": "t",
                    "model": model,
                    "country": "mexico",
                    "category": cat,
                    "table": "FAD",
                    "sel_mase": mase,
                    "sel_smape": mase * 10,
                    "sel_mae": mase * 100,
                    "sel_rmse": mase * 120,
                    "hold_mase": mase,
                    "hold_smape": mase * 10,
                    "hold_mae": mase * 100,
                    "hold_rmse": mase * 120,
                    "secs": 1.0,
                }
            )
    return pd.DataFrame(rows)


def test_ranking_and_table_and_summary() -> None:
    pytest.importorskip("darts")
    from vp_model import report, run_comparison

    df = _fake_results()

    rank = report.ranking(df)
    assert rank.index[0] == "SARIMA"  # menor MASE primero
    assert list(rank.index) == ["SARIMA", "ARIMA", "Naïve estacional"]

    win = report.winner_per_series(df)
    assert (win["model"] == "sarima").all()  # sarima gana ambas series

    tex = report.latex_table(df)
    assert r"\begin{table}" in tex and "SARIMA" in tex and r"\label{tab:comparacion_modelos}" in tex

    summ = run_comparison.summary(df)
    assert summ.index[0] == "sarima"


def test_intervals_probabilistic_quantiles() -> None:
    pytest.importorskip("darts")
    import numpy as np
    import pandas as pd
    from darts import TimeSeries

    from vp_model import intervals

    rng = np.random.default_rng(0)
    idx = pd.date_range("2000-01-01", periods=10, freq="MS")
    # TimeSeries estocástico: 200 muestras por paso.
    samples = np.stack([np.arange(10) + rng.normal(0, 1, 10) for _ in range(200)], axis=-1)[:, None, :]
    ts = TimeSeries.from_times_and_values(idx, samples)
    iv = intervals.probabilistic(ts)
    assert (iv.lower.values() <= iv.upper.values()).all()
    assert iv.mechanism.startswith("probabil")
