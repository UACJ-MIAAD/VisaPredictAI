"""Contrato de la caracterización feature-based (EDA de panel, FPP3 cap. 4).

Se omite sin el extra de modelado (statsmodels/scipy) o sin almacén.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("statsmodels")

from vp_model import dataset, features  # noqa: E402

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente")


def test_strengths_in_unit_range() -> None:
    ft, fs = features.stl_strengths(_clean())
    assert 0.0 <= ft <= 1.0 and 0.0 <= fs <= 1.0


def test_spectral_entropy_bounds() -> None:
    # Ruido blanco -> entropía ~1; señal pura (rampa) -> entropía baja.
    rng = np.random.default_rng(0)
    idx = pd.date_range("2000-01-01", periods=120, freq="MS")
    white = pd.Series(rng.normal(size=120), index=idx)
    ramp = pd.Series(np.arange(120, dtype="float64"), index=idx)
    assert features.spectral_entropy(white) > features.spectral_entropy(ramp)
    assert 0.0 <= features.spectral_entropy(ramp) <= 1.0


def test_ndiffs_trending_series_needs_differencing() -> None:
    assert features.ndiffs(_clean()) >= 1


def test_ljung_box_rejects_white_noise_for_trend() -> None:
    assert features.ljung_box_pvalue(_clean()) < 0.05  # no es ruido blanco
    rng = np.random.default_rng(1)
    idx = pd.date_range("2000-01-01", periods=120, freq="MS")
    assert features.ljung_box_pvalue(pd.Series(rng.normal(size=120), index=idx)) > 0.05


def test_feature_table_pilot_shape() -> None:
    ft = features.feature_table(table="FAD", block="family")
    assert len(ft) == 25
    # Hallazgo del EDA: estacionalidad casi nula en todo el panel de fechas de visa.
    assert (ft["seasonal_strength"] < 0.3).all()
    assert (ft["trend_strength"] > 0.5).mean() > 0.8  # tendencia fuerte en la mayoría


def test_advanced_separates_regime_from_point_anomalies() -> None:
    a = features.advanced("mexico", "F3", "FAD")
    # Los cambios de régimen son POCOS; las anomalías puntuales, más numerosas pero
    # acotadas. La distinción es justamente el fix del conteo crudo de "78 outliers".
    assert 0 <= a.n_changepoints <= 10
    assert a.n_changepoints < a.n_point_anomalies
    assert a.hurst > 0.5  # serie con tendencia/persistencia
    assert 0.0 <= a.perm_entropy <= 1.0
    # BDS detecta dependencia no lineal (p bajo) -> motiva los modelos no lineales.
    assert 0.0 <= a.bds_pvalue <= 1.0


def test_changepoints_detected_on_synthetic_step() -> None:
    import numpy as np

    idx = pd.date_range("2000-01-01", periods=120, freq="MS")
    step = pd.Series(np.r_[np.zeros(60), np.ones(60) * 100.0], index=idx)
    assert features.n_changepoints(step) >= 1


def test_catch22_returns_canonical_set() -> None:
    pytest.importorskip("pycatch22")
    c = features.catch22_vector("mexico", "F3", "FAD", catch24=True)
    assert len(c) == 24  # catch22 + media + desv. estándar
    assert all(isinstance(v, (int, float)) for v in c.values())


def test_validate_feature_table_contract() -> None:
    ft = features.feature_table(table="FAD", block="family")
    features.validate_feature_table(ft)  # no lanza
    bad = ft.copy()
    bad.loc[bad.index[0], "trend_strength"] = 2.0  # fuera de [0,1]
    with pytest.raises(AssertionError):
        features.validate_feature_table(bad)


def _clean() -> pd.Series:
    return features._clean("mexico", "F3", "FAD")
