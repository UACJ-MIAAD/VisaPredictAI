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
import pandas as pd
from darts import TimeSeries
from darts.metrics import mae, mase, rmse, smape

from vp_model.config import SEASONAL_PERIOD as SEASONAL_M
from vp_model.config import get_logger

log = get_logger("metrics")


def seasonal_naive_mae(values: np.ndarray, m: int = SEASONAL_M) -> float:
    """MAE del naïve estacional in-sample = denominador del MASE/MSIS. ÚNICA fuente.

    B4: una escala degenerada (serie de 1 punto, o constante) devuelve **NaN con
    warning**, no 1.0 — el fallback silencioso convertía el "MASE" en MAE en días
    (~10³) y contaminaba las medias agregadas sin dejar rastro. Los agregadores
    pandas (`mean()`) omiten NaN, así que la serie degenerada queda excluida del
    MASE pero conserva sus demás métricas.
    """
    v = np.asarray(values, dtype="float64")
    diffs = np.abs(v[m:] - v[:-m]) if len(v) > m else np.abs(np.diff(v))
    s = float(np.mean(diffs)) if len(diffs) else 0.0
    if np.isfinite(s) and s > 0:
        return s
    log.warning("escala naïve degenerada (n=%d, s=%r) — MASE indefinido para esta serie", len(v), s)
    return float("nan")


def naive_scale_before(full: pd.Series, cutoff, m: int = SEASONAL_M) -> float:
    """Escala naïve estacional sobre el tramo ANTERIOR a ``cutoff``, alineado por FECHA.

    El corte por fecha (no posicional ``full[:-len(g)]``) es robusto a series con huecos
    C/U: en el bloque empleo el corte posicional se desalinea. Leakage-free: solo pasado.
    """
    train = full[full.index < cutoff].astype("float64").to_numpy()
    return seasonal_naive_mae(train, m)


def _aligned(
    actual: TimeSeries, pred: TimeSeries, dates: pd.DatetimeIndex | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Pares (real, pronóstico) alineados por fecha; si ``dates`` se da, filtra a esas fechas.

    ``dates`` es el índice de observaciones F REALES: los meses que ``to_timeseries``
    interpola para dar continuidad al entrenamiento no son objetivo predictivo y NO
    deben puntuarse (B1 — misma máscara que ``eval_neuralforecast.eval_global_deep``).
    """
    common = actual.slice_intersect(pred)
    p = pred.slice_intersect(common)
    a = common.values().flatten()
    f = p.values().flatten()
    if dates is not None:
        m = common.time_index.isin(dates)
        a, f = a[m], f[m]
    return a, f


def compute(
    actual: TimeSeries,
    pred: TimeSeries,
    insample: TimeSeries,
    dates: pd.DatetimeIndex | None = None,
    scale: float | None = None,
) -> dict[str, float]:
    """MAE/RMSE/sMAPE/MASE de un pronóstico contra el real.

    ``insample`` es la serie de entrenamiento; MASE la usa para escalar por el error
    del naïve estacional dentro de la muestra (Hyndman & Koehler).

    ``dates``: si se da, las métricas se evalúan SOLO sobre esas fechas (observaciones
    F reales; B1). ``scale`` permite fijar el denominador del MASE desde fuera (p. ej.
    ``naive_scale_before`` sobre la serie F cruda, la misma fuente única de la vía
    global) — sin él se usa el naïve estacional de ``insample``.
    """
    if dates is None:
        common = actual.slice_intersect(pred)
        pred = pred.slice_intersect(common)
        return {
            "mae": float(mae(common, pred)),
            "rmse": float(rmse(common, pred)),
            "smape": float(smape(common, pred)),
            "mase": float(mase(common, pred, insample, m=SEASONAL_M)),
            "n": len(common),
        }
    a, f = _aligned(actual, pred, dates)
    if not len(a):
        return {"mae": float("nan"), "rmse": float("nan"), "smape": float("nan"), "mase": float("nan"), "n": 0}
    err = np.abs(a - f)
    mae_v = float(np.mean(err))
    s = scale if scale is not None else _seasonal_naive_mae(insample)
    return {
        "mae": mae_v,
        "rmse": float(np.sqrt(np.mean((a - f) ** 2))),
        # convención darts: sMAPE en 0–200 (no fracción), para que las tablas no mezclen escalas
        "smape": float(200.0 * np.mean(err / (np.abs(a) + np.abs(f) + 1e-9))),
        "mase": mae_v / s,
        "n": int(len(a)),
    }


def pi_coverage(
    actual: TimeSeries, lower: TimeSeries, upper: TimeSeries, dates: pd.DatetimeIndex | None = None
) -> float:
    """Cobertura empírica de un intervalo de predicción: fracción de reales dentro.

    Para un PI al 95% bien calibrado debería rondar 0.95. ``dates`` restringe la
    medición a observaciones F reales (B1).
    """
    a, lo = _aligned(actual, lower, dates)
    _, hi = _aligned(actual, upper, dates)
    if not len(a):
        return float("nan")
    return float(((a >= lo) & (a <= hi)).mean())


def _seasonal_naive_mae(insample: TimeSeries, m: int = SEASONAL_M) -> float:
    """Escala del MASE/MSIS sobre una ``TimeSeries`` de darts (delega en la fuente única)."""
    return seasonal_naive_mae(insample.values().flatten(), m)


def interval_score(
    actual: TimeSeries,
    lower: TimeSeries,
    upper: TimeSeries,
    alpha: float = 0.05,
    dates: pd.DatetimeIndex | None = None,
) -> float:
    """Interval score de Gneiting-Raftery para un intervalo al (1-alpha) (menor es mejor).

    IS = (u - l) + (2/alpha)(l - y)·1{y<l} + (2/alpha)(y - u)·1{y>u}: penaliza la
    amplitud y, con fuerza 2/alpha, cada real que cae fuera del intervalo.
    ``dates`` restringe la medición a observaciones F reales (B1).
    """
    a, lo = _aligned(actual, lower, dates)
    _, hi = _aligned(actual, upper, dates)
    if not len(a):
        return float("nan")
    width = hi - lo
    below = (2.0 / alpha) * (lo - a) * (a < lo)
    above = (2.0 / alpha) * (a - hi) * (a > hi)
    return float(np.mean(width + below + above))


def msis(
    actual: TimeSeries,
    lower: TimeSeries,
    upper: TimeSeries,
    insample: TimeSeries,
    alpha: float = 0.05,
    dates: pd.DatetimeIndex | None = None,
    scale: float | None = None,
) -> float:
    """Mean Scaled Interval Score (M5): interval score escalado por el naïve estacional."""
    s = scale if scale is not None else _seasonal_naive_mae(insample)
    return interval_score(actual, lower, upper, alpha, dates) / s


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
