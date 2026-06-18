"""Contrato del manejo SOTA de valores faltantes (MNAR)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("statsmodels")

from vp_model import dataset  # noqa: E402
from vp_model import missingness as miss

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")


def test_gap_runs_counts_consecutive() -> None:
    assert miss._gap_runs(np.array([0, 1, 1, 0, 1, 0, 0], dtype=bool)) == [2, 1]
    assert miss._gap_runs(np.array([0, 0, 0], dtype=bool)) == []


def test_profile_consistency() -> None:
    p = miss.profile("china", "F1", "FAD")
    assert p.n_missing == p.n_months - p.n_observed
    assert p.max_gap_run >= p.median_gap_run >= 0


def test_kalman_fills_all_and_preserves_observed() -> None:
    raw = dataset.load_series("china", "F1", "FAD")
    imp = miss.kalman_impute(raw)
    assert imp.isna().sum() == 0
    grid = miss._raw_monthly(raw)
    m = grid.notna()
    assert np.allclose(imp[m].to_numpy(), grid[m].to_numpy(), rtol=1e-6)  # no altera observados


def test_masking_features_causal_and_consistent() -> None:
    raw = dataset.load_series("china", "F1", "FAD")
    mf = miss.masking_features(raw)
    assert set(mf.columns) == {"observed", "months_since_obs"}
    # donde hay observación, el contador es 0; el máximo iguala la mayor corrida de huecos
    assert (mf.loc[mf["observed"] == 1, "months_since_obs"] == 0).all()
    assert mf["months_since_obs"].max() == miss.profile("china", "F1", "FAD").max_gap_run


def test_masking_no_lookahead() -> None:
    # months_since_obs en t depende solo del pasado: truncar el futuro no cambia el prefijo.
    raw = dataset.load_series("mexico", "F3", "FAD")
    full = miss.masking_features(raw)
    trunc = miss.masking_features(raw.iloc[:-12])
    n = len(trunc)
    assert (full["months_since_obs"].to_numpy()[:n] == trunc["months_since_obs"].to_numpy()).all()
