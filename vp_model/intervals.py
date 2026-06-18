"""Intervalos de predicción al 95% (US-E3): dos mecanismos funcionales.

1. Probabilístico nativo: ARIMA/SARIMA y DeepAR emiten muestras (darts
   ``num_samples``) de las que se leen los cuantiles 2.5% y 97.5%. Cubre los modelos
   estadísticos y el de red neuronal.
2. Conforme (split conformal): model-agnostic; usa los errores absolutos del tramo de
   calibración para fijar un semiancho con cobertura garantizada bajo intercambiabilidad.

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
    point_forecast: TimeSeries, calib_actual: TimeSeries, calib_pred: TimeSeries, alpha: float = ALPHA
) -> Interval:
    """Split conformal: PI = pronóstico ± cuantil(1-alpha) de |error| en calibración.

    Model-agnostic: sirve para cualquier modelo del catálogo a partir de su backtest.
    El semiancho es el cuantil empírico de los residuales absolutos de calibración,
    con la pequeña corrección de tamaño finito (n+1).
    """
    common = calib_actual.slice_intersect(calib_pred)
    resid = np.abs(common.values().flatten() - calib_pred.slice_intersect(common).values().flatten())
    n = len(resid)
    # Cuantil conforme con corrección finita: ceil((n+1)(1-alpha))/n.
    q_level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
    half = float(np.quantile(resid, q_level))
    return Interval(point_forecast - half, point_forecast + half, "conforme")


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
