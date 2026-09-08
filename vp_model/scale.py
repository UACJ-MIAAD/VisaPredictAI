"""E0 · La escala naïve del MASE — FUENTE ÚNICA, sin dependencias de modelado.

El denominador del MASE (y del MSIS) es el MAE del naïve estacional in-sample
(Hyndman & Koehler 2006). La fórmula vivía copiada en cuatro sitios: la canónica en
``vp_model.metrics``, una réplica declarada en ``experiments/run_statsforecast.py`` y dos
privadas en ``experiments/improve_tabpfn.py`` e ``improve_timesfm.py``. Las réplicas
existían por una razón real: ``vp_model.metrics`` importa ``darts`` a nivel de módulo y
esos experimentos corren en entornos sin el extra ``model``. Este módulo solo necesita
numpy, pandas y ``vp_model.config`` (dependency-light), así que la razón desaparece y con
ella las copias — incluida la deriva que arrastraban (ver ``seasonal_naive_mae``).

Semántica única, sin excepciones por consumidor:

* **Historia insuficiente** (``len(v) <= m``): se usan diferencias de orden 1. Con menos
  de dos puntos no hay ninguna diferencia y la escala queda **indefinida**.
* **Escala cero** (serie constante) y **valores no finitos** (NaN/inf): **indefinida**.
  La resta se hace bajo ``np.errstate(invalid="ignore")``: ``inf - inf`` emitía un
  ``RuntimeWarning`` de numpy que no cambia el resultado (sigue siendo ``NaN``) pero que,
  bajo el contrato de warnings de la suite (``error``), habría hecho **reventar** a quien
  puntuara una serie con un infinito. El aviso explícito del log sí se conserva.
* **Indefinida** significa ``NaN`` con aviso en el log, nunca ``1.0``. Un ``1.0``
  silencioso no deja rastro y convierte el "MASE" en MAE en días (~10³), contaminando
  las medias agregadas. Los agregadores nan-aware omiten NaN, así que la serie degenerada
  queda **excluida** del MASE y conserva sus demás métricas.
* **Periodo estacional inválido** (``m`` no entero, booleano o ``< 1``): ``ValueError``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vp_model.config import SEASONAL_PERIOD as SEASONAL_M
from vp_model.config import get_logger

__all__ = ["SEASONAL_M", "naive_scale_before", "seasonal_naive_mae"]

log = get_logger("scale")


def _validar_periodo(m: object) -> int:
    """``m`` es un entero ≥ 1. Antes: ``m=0`` reventaba dentro de numpy con un error de
    difusión y ``m<0`` calculaba una escala silenciosamente equivocada."""
    if isinstance(m, bool) or not isinstance(m, (int, np.integer)):
        raise ValueError(f"periodo estacional inválido: {m!r} (se espera un entero ≥ 1)")
    if m < 1:
        raise ValueError(f"periodo estacional inválido: {m!r} (se espera un entero ≥ 1)")
    return int(m)


def seasonal_naive_mae(values: np.ndarray, m: int = SEASONAL_M) -> float:
    """MAE del naïve estacional in-sample = denominador del MASE/MSIS. ÚNICA fuente.

    B4: una escala degenerada (serie de 1 punto, o constante) devuelve **NaN con
    warning**, no 1.0 — el fallback silencioso convertía el "MASE" en MAE en días
    (~10³) y contaminaba las medias agregadas sin dejar rastro. Los agregadores
    pandas (`mean()`) omiten NaN, así que la serie degenerada queda excluida del
    MASE pero conserva sus demás métricas.
    """
    periodo = _validar_periodo(m)
    v = np.asarray(values, dtype="float64")
    with np.errstate(invalid="ignore"):  # inf - inf ⇒ NaN, que es el resultado buscado
        diffs = np.abs(v[periodo:] - v[:-periodo]) if len(v) > periodo else np.abs(np.diff(v))
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
