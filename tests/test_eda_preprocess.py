"""Contrato del EDA y el preprocesamiento (US-C1, US-C2, US-C3).

Requiere el almacén DuckDB; se omite si falta (igual que test_dataset).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vp_model import dataset, eda, preprocess

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente (corre `make db`)")


def test_profile_gaps_consistent() -> None:
    p = eda.profile_series("china", "F1", "FAD")
    assert p.n_gaps == p.span_months - p.n_obs
    assert 0.0 < p.continuity <= 1.0


def test_pilot_fad_family_all_evaluable() -> None:
    df = eda.profile_all(table="FAD", block="family")
    assert len(df) == 25
    assert df["evaluable"].all()


def test_priority_date_series_not_stationary_in_level() -> None:
    # Una serie de fechas que avanza no es estacionaria en nivel -> hay que diferenciar.
    st = eda.stationarity("mexico", "F3", "FAD")
    assert st["verdict"] in {"difference", "mixed"}
    # DF-GLS (alta potencia bajo tendencia fuerte) también reporta no estacionariedad en nivel.
    dfgls = float(st["dfgls_pvalue"])
    assert 0.0 <= dfgls <= 1.0
    assert dfgls >= 0.05  # no rechaza raíz unitaria en el nivel


def test_regular_monthly_preserves_observed_and_caps_long_gaps() -> None:
    raw = dataset.load_series("china", "F1", "FAD")
    reg = preprocess.to_regular_monthly(raw)
    assert reg.index.freq == "MS"
    assert np.allclose(reg.loc[raw.index].to_numpy(), raw.to_numpy())

    s = pd.Series([0.0, np.nan, np.nan, np.nan, np.nan, 100.0], index=pd.date_range("2020-01-01", periods=6, freq="MS"))
    # 4 NaN consecutivos > max_gap=3 -> se dejan TODOS como NaN (todo-o-nada).
    assert preprocess.to_regular_monthly(s, max_gap=3).isna().sum() == 4
    # 2 NaN <= max_gap -> se interpolan.
    s2 = pd.Series([0.0, np.nan, np.nan, 30.0], index=pd.date_range("2020-01-01", periods=4, freq="MS"))
    assert preprocess.to_regular_monthly(s2, max_gap=3).isna().sum() == 0


def test_difference_undifference_roundtrip() -> None:
    # AD7: Standardizer se eliminó (el escalado real es el darts Scaler de
    # feature_builder, con su propio test de leakage). El contrato de preprocess
    # ahora es la pareja difference/undifference (AD2).
    full = dataset.load_series("mexico", "F3", "FAD").astype("float64")
    d = preprocess.difference(full)
    back = preprocess.undifference(d.iloc[1:], last_level=float(full.iloc[0]))
    assert np.allclose(back.to_numpy(), full.iloc[1:].to_numpy())


def test_calendar_features_shape() -> None:
    idx = pd.date_range("2020-01-01", periods=12, freq="MS")
    feats = preprocess.calendar_features(idx)
    assert len(feats) == 12
    assert (feats["month_sin"].abs() <= 1).all()
