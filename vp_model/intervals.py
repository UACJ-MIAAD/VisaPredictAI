"""Intervalos de predicción al 95% (US-E3): dos mecanismos funcionales.

1. Probabilístico nativo: ARIMA/SARIMA y DeepAR emiten muestras (darts
   ``num_samples``) de las que se leen los cuantiles 2.5% y 97.5%. Cubre los modelos
   estadísticos y el de red neuronal.
2. Conforme (split conformal): model-agnostic; usa los errores absolutos del tramo de
   calibración para fijar un semiancho. La garantía teórica de cobertura del conforme exige
   INTERCAMBIABILIDAD, que estas series (con tendencia y cambios de régimen) NO satisfacen;
   por eso se reporta la cobertura nominal 95% como objetivo y la EMPÍRICA medida (≈0.89 FAD,
   ≈0.83 DFF por colas pesadas), no una cobertura garantizada. Ver ``metrics.pi_coverage``.

Nota: el Monte Carlo dropout que contemplaba el Anteproyecto NO está disponible en
darts 0.44.1 para un RNN determinista (``num_samples>1`` exige un modelo
probabilístico); el camino de incertidumbre para redes lo cubre DeepAR vía su
verosimilitud. La cobertura empírica se mide con ``metrics.pi_coverage``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from darts import TimeSeries

from vp_model.config import ALPHA


@dataclass(frozen=True)
class Interval:
    lower: TimeSeries
    upper: TimeSeries
    mechanism: str


def conformal(
    point_forecast: TimeSeries,
    calib_actual: TimeSeries,
    calib_pred: TimeSeries,
    alpha: float = ALPHA,
    calib_dates=None,
) -> Interval:
    """Split conformal: PI = pronóstico ± cuantil(1-alpha) de |error| en calibración.

    Model-agnostic: sirve para cualquier modelo del catálogo a partir de su backtest.
    El semiancho es el cuantil empírico de los residuales absolutos de calibración,
    con la pequeña corrección de tamaño finito (n+1). ``calib_dates`` restringe la
    calibración a observaciones F reales (B1): los residuales sobre meses interpolados
    son artificialmente pequeños y estrecharían el intervalo.
    """
    common = calib_actual.slice_intersect(calib_pred)
    resid = np.abs(common.values().flatten() - calib_pred.slice_intersect(common).values().flatten())
    if calib_dates is not None:
        resid = resid[common.time_index.isin(calib_dates)]
    n = len(resid)
    if n == 0:
        raise ValueError("sin residuales de calibración sobre fechas F reales")
    # Cuantil conforme con corrección finita: ceil((n+1)(1-alpha))/n.
    q_level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
    # AN7: method="higher" (order statistic) — linear interpolation is anti-conservative
    # with small n and breaks the finite-sample guarantee the (n+1) correction buys.
    half = float(np.quantile(resid, q_level, method="higher"))
    return Interval(point_forecast - half, point_forecast + half, "conforme")


def aci_alpha(hits, alpha0: float = ALPHA, gamma: float = 0.05) -> float:
    """Effective miscoverage level after Adaptive Conformal Inference updates (AN4).

    Pure function (Gibbs & Candès 2021): starting at ``alpha0``, replay the online
    update ``alpha_{t+1} = alpha_t + gamma * (alpha0 - err_t)`` over the observed
    ``hits`` sequence (truthy = the realized value fell inside the interval, so
    ``err_t = 0``; falsy = miss, ``err_t = 1``). A run of misses LOWERS alpha
    (wider future intervals); a run of hits raises it (narrower). The caller feeds
    the per-series hit history from the prospective ledger, in chronological order,
    and uses the returned level as the conformal alpha for the NEXT forecast.

    Returns ``alpha0`` unchanged for an empty history. The result is clamped to
    [1e-3, 0.999] so downstream quantiles stay well-defined.
    """
    a = float(alpha0)
    for h in hits:
        a += gamma * (alpha0 - (0.0 if h else 1.0))
    return min(max(a, 1e-3), 0.999)


def jeffreys_ci(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Jeffreys interval for a binomial proportion (AN7): Beta(k+1/2, n-k+1/2) quantiles.

    Standard boundary convention: lower bound is 0 when ``k == 0`` and upper bound is
    1 when ``k == n``. Every empirical coverage this project reports should carry this
    CI together with ``n`` (a coverage without sample size is not a claim).
    """
    if n <= 0:
        raise ValueError("jeffreys_ci requires n > 0")
    if not 0 <= k <= n:
        raise ValueError(f"k={k} out of range for n={n}")
    from scipy.stats import beta

    tail = (1.0 - conf) / 2.0
    lo = 0.0 if k == 0 else float(beta.ppf(tail, k + 0.5, n - k + 0.5))
    hi = 1.0 if k == n else float(beta.ppf(1.0 - tail, k + 0.5, n - k + 0.5))
    return lo, hi


def probabilistic(samples: TimeSeries, alpha: float = ALPHA) -> Interval:
    """Banda a partir de un pronóstico estocástico (ARIMA/SARIMA/DeepAR o MC dropout).

    ``samples`` es la salida de ``predict(num_samples>1)``; se leen los cuantiles.
    """
    if samples.n_samples <= 1:
        raise ValueError("se requieren múltiples muestras (predict(num_samples>1))")
    lower = samples.quantile(alpha / 2)
    upper = samples.quantile(1 - alpha / 2)
    mech = "probabilístico nativo"
    return Interval(lower, upper, mech)


def demo() -> None:
    """Self-check: el conforme cubre ~ (1-alpha) en datos intercambiables."""
    import pandas as pd

    rng = np.random.default_rng(0)
    idx = pd.date_range("2000-01-01", periods=300, freq="MS")
    truth = np.arange(300, dtype="float64") * 10
    noise = rng.normal(0, 50, 300)
    actual = TimeSeries.from_series(pd.Series(truth + noise, index=idx))
    pred = TimeSeries.from_series(pd.Series(truth, index=idx))  # pronóstico = señal sin ruido

    calib_a, test_a = actual[:200], actual[200:]
    calib_p, test_p = pred[:200], pred[200:]
    iv = conformal(test_p, calib_a, calib_p)
    from vp_model.metrics import pi_coverage

    cov = pi_coverage(test_a, iv.lower, iv.upper)
    assert 0.88 <= cov <= 1.0, f"cobertura conforme fuera de rango: {cov}"
    print(f"OK — PI conforme al 95%: cobertura empírica fuera de muestra = {cov:.2%}")


if __name__ == "__main__":
    demo()
