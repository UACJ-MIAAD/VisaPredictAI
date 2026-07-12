"""US-E5 — regresión CIENTÍFICA del protocolo de métricas sobre fixtures sintéticas.

Congela PROPIEDADES del protocolo (no cifras accidentales de una campaña):

  * piso random-walk: naïve-1 sobre una serie congelada da MASE = 0 en los F;
  * aritmética del MASE: sobre tendencia limpia de paso constante, naïve-1 da
    MASE = 1/12 exacto (denominador estacional m=12) y MASE1 = 1; drift da ~0;
  * metamórficos: desplazar TODA la serie en el tiempo no cambia el MASE, y
    reescalar los valores tampoco (el MAE sí escala — eso también se afirma);
  * el fix causal F1 TIENE EFECTO: sobre la fixture con un hueco cruzando
    orígenes, los pronósticos del motor (rejilla causal) difieren de los de la
    transformación antigua (bidireccional) exactamente en la ventana del hueco.

Las series son sintéticas y ``dataset.load_series`` se monkeypatcha: el archivo no
requiere el almacén DuckDB y congela el PROTOCOLO, no el panel. Sin el extra
``model`` el archivo se omite de la colección vía ``conftest.collect_ignore``
(mismo patrón que test_eda_preprocess).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vp_model import config, dataset, metrics, walkforward

N = 96  # >= MIN_TRAIN['FAD'](60) + HOLDOUT(24) + MIN_BACKTEST_BUFFER(6)


def _idx(n: int = N, start: str = "2010-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="MS")


def _patch(monkeypatch: pytest.MonkeyPatch, series: pd.Series) -> None:
    monkeypatch.setattr(dataset, "load_series", lambda *a, **k: series)


def _linear_trend(start: str = "2010-01-01", scale: float = 1.0) -> pd.Series:
    return pd.Series((np.arange(N, dtype="float64") * 30.0 + 1000.0) * scale, index=_idx(start=start))


# ------------------------------------------------------------------ pisos/aritmética
def test_naive1_on_frozen_series_scores_zero_mase(monkeypatch: pytest.MonkeyPatch) -> None:
    """RW congelado: la serie sube 60 meses y queda CONGELADA; naïve-1 clava cada F.

    Es el piso que ancla la narrativa canónica (a h=1 el random walk es piso y techo
    en meses congelados). Incluye un hueco corto para que la máscara F-only trabaje.
    """
    idx = _idx()
    vals = np.where(np.arange(N) < 60, 1000.0 + 30.0 * np.arange(N), 1000.0 + 30.0 * 59)
    frozen = pd.Series(vals, index=idx).drop(idx[65:67])  # hueco de 2 meses en la meseta
    _patch(monkeypatch, frozen)
    r = walkforward.backtest("naive1", "x", "F1", "FAD")
    assert r.selection["mase"] == 0.0 and r.holdout["mase"] == 0.0
    assert r.holdout["mae"] == 0.0
    assert r.holdout["n"] == config.HOLDOUT  # el hueco cae en selección; el hold-out puntúa 24 F


def test_naive1_mase_arithmetic_on_clean_trend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tendencia limpia de paso 30: naïve-1 yerra 30/mes; el denominador estacional
    (m=12) vale 360 ⇒ MASE = 1/12 EXACTO y MASE1 = 1. Drift extrapola el paso ⇒ ~0.

    Si alguien cambiara el m del denominador, la ventana del scale o la máscara,
    esta aritmética truena — congela el PROTOCOLO, no una cifra de campaña.
    """
    trend = _linear_trend()
    _patch(monkeypatch, trend)
    r = walkforward.backtest("naive1", "x", "F1", "FAD")
    assert r.holdout["mase"] == pytest.approx(1.0 / 12.0, rel=1e-12)
    assert r.selection["mase"] == pytest.approx(1.0 / 12.0, rel=1e-12)
    assert r.holdout["mase1"] == pytest.approx(1.0, rel=1e-12)

    d = walkforward.backtest("drift", "x", "F1", "FAD")
    assert d.holdout["mase"] == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------- metamórficos E5
@pytest.mark.parametrize("model_name", ["naive1", "drift"])
def test_mase_invariant_to_time_shift(model_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Desplazar TODA la serie (+7 meses de calendario, mismos valores) no cambia el
    MASE relativo: el protocolo es posicional sobre los valores, no sobre el año."""
    base = _linear_trend(start="2010-01-01")
    shifted = _linear_trend(start="2010-08-01")  # +7 meses (cruza el año fiscal)
    _patch(monkeypatch, base)
    r0 = walkforward.backtest(model_name, "x", "F1", "FAD")
    _patch(monkeypatch, shifted)
    r1 = walkforward.backtest(model_name, "x", "F1", "FAD")
    for part in ("selection", "holdout"):
        assert getattr(r0, part)["mase"] == pytest.approx(getattr(r1, part)["mase"], rel=1e-12)
        assert getattr(r0, part)["n"] == getattr(r1, part)["n"]


def test_mase_invariant_to_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reescalar los valores (×3.7) deja el MASE idéntico (numerador y denominador
    escalan por igual) mientras el MAE escala ×3.7 — ambas cosas se afirman."""
    base = _linear_trend(scale=1.0)
    scaled = _linear_trend(scale=3.7)
    _patch(monkeypatch, base)
    r0 = walkforward.backtest("naive1", "x", "F1", "FAD")
    _patch(monkeypatch, scaled)
    r1 = walkforward.backtest("naive1", "x", "F1", "FAD")
    assert r1.holdout["mase"] == pytest.approx(r0.holdout["mase"], rel=1e-12)
    assert r1.holdout["mae"] == pytest.approx(3.7 * r0.holdout["mae"], rel=1e-12)


def test_metric_level_scale_invariance() -> None:
    """La invariancia también vale a nivel métrica (metrics.compute con máscara)."""
    from darts import TimeSeries

    idx = _idx(48)
    y = pd.Series(np.arange(48, dtype="float64") * 30 + 1000, index=idx)
    ts = TimeSeries.from_series(y)
    biased = ts[36:] + 60.0
    m1 = metrics.compute(biased, ts[36:], ts[:36], dates=idx[36:])
    ts_s = ts * 3.7
    m2 = metrics.compute(ts_s[36:] + 60.0 * 3.7, ts_s[36:], ts_s[:36], dates=idx[36:])
    assert m2["mase"] == pytest.approx(m1["mase"], rel=1e-12)
    assert m2["mase1"] == pytest.approx(m1["mase1"], rel=1e-12)
    assert m2["mae"] == pytest.approx(3.7 * m1["mae"], rel=1e-12)


# ------------------------------------------------------- el fix F1 tiene efecto real
def test_causal_fix_has_effect_on_gap_crossing_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture con hueco cruzando orígenes: los pronósticos del motor (rejilla causal
    LOCF) DIFIEREN de los de la transformación antigua (interpolación bidireccional)
    exactamente en la ventana del hueco — y coinciden fuera de ella. Demuestra que el
    fix F1 cambia los insumos donde fugaban, y solo ahí.
    """
    idx = _idx()
    g0, gap_len = 70, 3
    vals = np.arange(N, dtype="float64") * 30.0 + 1000.0 + 40.0 * np.sin(np.arange(N) / 6.0)
    raw = pd.Series(vals, index=idx).drop(idx[g0 : g0 + gap_len])

    _patch(monkeypatch, raw)
    _ts, fc = walkforward.run_forecasts("naive1", "x", "F1", "FAD")
    causal = pd.Series(fc.values().flatten(), index=fc.time_index)

    # Transformación ANTIGUA (contrafactual, inline): rampa lineal bidireccional.
    leaky_grid = raw.reindex(idx).astype("float64").interpolate(method="linear", limit_area="inside")
    leaky = leaky_grid.shift(1).loc[causal.index]  # naïve-1 = valor del mes previo

    # naïve-1 lee la rejilla en t-1 ⇒ los pronósticos afectados son los de los meses
    # (g0+1 .. g0+gap_len]; el último (el bracket derecho) es un mes F REAL puntuado.
    affected = idx[g0 + 1 : g0 + gap_len + 1]
    outside = causal.index.difference(affected)
    assert not np.allclose(causal.loc[affected], leaky.loc[affected]), (
        "el fix causal debería cambiar los pronósticos dentro de la ventana del hueco"
    )
    assert np.allclose(causal.loc[outside], leaky.loc[outside]), (
        "fuera de la ventana del hueco ambas rejillas deben coincidir"
    )
    # y el pronóstico afectado en el bracket derecho SÍ se puntúa (es fecha F real):
    assert idx[g0 + gap_len] in set(raw.index)
