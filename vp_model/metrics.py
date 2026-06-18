"""Métricas de error puntuales y probabilísticas.

Puntuales (sobre el nivel): MAE, RMSE, sMAPE y MASE escalada por el naïve estacional
(Hyndman & Koehler 2006), vía ``darts.metrics``. Probabilísticas (sobre la
distribución/intervalo predictivo), implementadas con sus fórmulas canónicas:
  * CRPS — Continuous Ranked Probability Score, regla de puntuación estrictamente
    propia que evalúa la distribución completa (Gneiting & Raftery 2007); se reduce
    al MAE para un pronóstico puntual.
  * MSIS — Mean Scaled Interval Score, métrica oficial de incertidumbre del M5
    (Makridakis et al. 2022): penaliza intervalos demasiado anchos O estrechos y
    escala por el error naïve, comparable entre series.
  * pinball — pérdida cuantílica (quantile loss), score propio por cuantil.
Todas se evalúan SOLO sobre observaciones con fecha (estado F).
"""

from __future__ import annotations

import numpy as np
from darts import TimeSeries
from darts.metrics import mae, mase, rmse, smape

from vp_model.config import SEASONAL_PERIOD as SEASONAL_M


def compute(actual: TimeSeries, pred: TimeSeries, insample: TimeSeries) -> dict[str, float]:
    """MAE/RMSE/sMAPE/MASE de un pronóstico contra el real.

    ``insample`` es la serie de entrenamiento; MASE la usa para escalar por el error
    del naïve estacional dentro de la muestra (Hyndman & Koehler).
    """
    common = actual.slice_intersect(pred)
    pred = pred.slice_intersect(common)
    return {
        "mae": float(mae(common, pred)),
        "rmse": float(rmse(common, pred)),
        "smape": float(smape(common, pred)),
        "mase": float(mase(common, pred, insample, m=SEASONAL_M)),
        "n": len(common),
    }


def pi_coverage(actual: TimeSeries, lower: TimeSeries, upper: TimeSeries) -> float:
    """Cobertura empírica de un intervalo de predicción: fracción de reales dentro.

    Para un PI al 95% bien calibrado debería rondar 0.95.
    """
    a = actual.slice_intersect(lower).values().flatten()
    lo = lower.slice_intersect(actual).values().flatten()
    hi = upper.slice_intersect(actual).values().flatten()
    return float(((a >= lo) & (a <= hi)).mean())


def _seasonal_naive_mae(insample: TimeSeries, m: int = SEASONAL_M) -> float:
    """Escala del MASE/MSIS: MAE del naïve estacional dentro de la muestra."""
    v = insample.values().flatten()
    if len(v) <= m:
        return float(np.mean(np.abs(np.diff(v)))) or 1.0
    return float(np.mean(np.abs(v[m:] - v[:-m]))) or 1.0


def interval_score(actual: TimeSeries, lower: TimeSeries, upper: TimeSeries, alpha: float = 0.05) -> float:
    """Interval score de Gneiting-Raftery para un intervalo al (1-alpha) (menor es mejor).

    IS = (u - l) + (2/alpha)(l - y)·1{y<l} + (2/alpha)(y - u)·1{y>u}: penaliza la
    amplitud y, con fuerza 2/alpha, cada real que cae fuera del intervalo.
    """
    a = actual.slice_intersect(lower).values().flatten()
    lo = lower.slice_intersect(actual).values().flatten()
    hi = upper.slice_intersect(actual).values().flatten()
    width = hi - lo
    below = (2.0 / alpha) * (lo - a) * (a < lo)
    above = (2.0 / alpha) * (a - hi) * (a > hi)
    return float(np.mean(width + below + above))


def msis(actual: TimeSeries, lower: TimeSeries, upper: TimeSeries, insample: TimeSeries, alpha: float = 0.05) -> float:
    """Mean Scaled Interval Score (M5): interval score escalado por el naïve estacional."""
    return interval_score(actual, lower, upper, alpha) / _seasonal_naive_mae(insample)


def crps(actual: TimeSeries, samples: TimeSeries) -> float:
    """CRPS empírico promedio a partir de un pronóstico por muestras (estocástico).

    CRPS = E|X - y| - 0.5·E|X - X'| (forma de energía), estimado con las muestras de
    cada paso. Se reduce al MAE si el pronóstico es determinista.
    """
    a = actual.slice_intersect(samples)
    y = a.values().flatten()
    sm = samples.slice_intersect(a).all_values()  # (tiempo, componentes, muestras)
    vals = []
    for t in range(len(y)):
        x = sm[t, 0, :]
        term1 = np.mean(np.abs(x - y[t]))
        term2 = 0.5 * np.mean(np.abs(x[:, None] - x[None, :]))
        vals.append(term1 - term2)
    return float(np.mean(vals))


def pinball(actual: TimeSeries, quantile_pred: TimeSeries, q: float) -> float:
    """Pérdida pinball (quantile loss) en el cuantil q (score propio por cuantil)."""
    a = actual.slice_intersect(quantile_pred).values().flatten()
    f = quantile_pred.slice_intersect(actual).values().flatten()
    err = a - f
    return float(np.mean(np.maximum(q * err, (q - 1.0) * err)))


def demo() -> None:
    """Self-check: métricas perfectas valen 0; CRPS<=MAE puntual; MSIS finito."""
    import pandas as pd

    idx = pd.date_range("2000-01-01", periods=48, freq="MS")
    y = pd.Series(np.arange(48, dtype="float64") * 30 + 1000, index=idx)
    ts = TimeSeries.from_series(y)
    insample, actual = ts[:36], ts[36:]
    perfect = compute(actual, actual, insample)
    assert perfect["mae"] == 0.0 and perfect["smape"] == 0.0
    assert perfect["mase"] == 0.0

    lo = actual - 1000
    hi = actual + 1000
    assert pi_coverage(actual, lo, hi) == 1.0
    assert pi_coverage(actual, actual + 1, actual + 2) == 0.0  # real fuera del intervalo

    # Interval score (a igual amplitud): el que cubre debe puntuar mejor que el que no.
    is_cover = interval_score(actual, actual - 5, actual + 5)  # ancho 10, cubre
    is_miss = interval_score(actual, actual + 10, actual + 20)  # ancho 10, no cubre
    assert is_miss > is_cover, "no cubrir debe penalizarse más"
    assert msis(actual, lo, hi, insample) > 0

    # CRPS: muestras concentradas en el valor real -> CRPS ~ 0; sesgadas -> mayor.
    n_s = 200
    vals = np.repeat(actual.values(), n_s, axis=1)[:, None, :]  # (t, 1, muestras) = real exacto
    exact = TimeSeries.from_times_and_values(actual.time_index, vals)
    assert crps(actual, exact) < 1e-6
    biased = TimeSeries.from_times_and_values(actual.time_index, vals + 500.0)
    assert crps(actual, biased) > crps(actual, exact)

    # pinball en la mediana = 0.5 * MAE.
    assert abs(pinball(actual, actual + 10, 0.5) - 0.5 * 10) < 1e-6
    print(f"OK — puntuales=0; IS cubre<no-cubre ({is_cover:.0f}<{is_miss:.0f}); CRPS exacto~0; pinball OK")


if __name__ == "__main__":
    demo()
