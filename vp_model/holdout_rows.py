"""Las filas largas del hold-out: lo que el walk-forward ya calcula, en forma transportable (§8.7).

`model_comparison_*21.csv` guarda **una fila por serie** con las métricas agregadas. Los
pronósticos punto a punto existen en memoria durante `walkforward.backtest()` y se tiraban. Por eso
`ets` y `theta` no podían exportarse: `AutoETS`/`AutoTheta` rechazan
`historical_forecasts(retrain=False)`, así que el exportador no podía recalcularlos —**50 eventos
gobernados fallidos por corrida**, con el exportador terminando en verde—.

Este módulo es **puro**: recibe objetos ya computados y devuelve filas. No ajusta, no mide, no
escribe. La salvaguarda de §8.7.4 —*instrumentar no puede cambiar el cálculo*— es estructural aquí:
no hay forma de que estas filas alteren una métrica, porque no tocan el camino que las produce.

★ **Dos reglas del contrato que parecen detalles y no lo son:**

1. **Las filas nacen de ``hold_fc``, nunca del cruce con los reales.** ``slice_intersect`` sirve
   para *leer* ``y_true``, pero si gobernara qué filas existen, los meses **no evaluables**
   desaparecerían y el artefacto perdería **la razón exacta** por la que una fila no puntúa. Se
   emiten los 24 objetivos siempre, con ``observed=False`` y ``y_true=None`` donde no hay fecha F.
2. **``origin`` se deriva por periodo mensual**, no restando días. Los meses no miden lo mismo, y
   ``target - 30 días`` produciría orígenes que no existen en la rejilla.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:  # pragma: no cover
    from darts import TimeSeries

#: El walk-forward oficial rueda a un paso, sin salto y quedándose el último punto.
FORECAST_HORIZON = 1
STRIDE = 1
LAST_POINTS_ONLY = True


class HoldoutRowsError(ValueError):
    """No se puede construir una fila fiel: falta información o la escala no es utilizable."""


def _origin_of(target: pd.Timestamp) -> pd.Timestamp:
    """El origen de un pronóstico a ``h=1``: el mes ANTERIOR, por periodo.

    ⚠️ No ``target - Timedelta(days=30)``: en la rejilla mensual canónica eso cae fuera de la malla
    la mitad de las veces. El periodo es la unidad real de esta serie.
    """
    return (target.to_period("M") - 1).to_timestamp()


def build_rows(
    *,
    hold_fc: TimeSeries,
    actual: TimeSeries,
    fdates: pd.Index,
    scale: float,
    model: str,
    table: str,
    country: str,
    category: str,
) -> list[dict[str, Any]]:
    """Las filas del hold-out para una (serie, modelo). Una por objetivo pronosticado.

    ``scale`` es la escala del MASE que el propio ``backtest`` ya calculó: se **transporta**, no se
    recalcula, para que el consumidor no pueda derivar una distinta.
    """
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise HoldoutRowsError(
            f"escala MASE no utilizable para {table}/{country}/{category}/{model}: {scale!r}. "
            "Debe ser finita y positiva; una escala degenerada convierte el MASE en otra métrica"
        )
    evaluables = set(pd.DatetimeIndex(fdates))
    reales = {pd.Timestamp(t): float(v) for t, v in zip(actual.time_index, actual.values().ravel(), strict=False)}

    filas: list[dict[str, Any]] = []
    for t, v in zip(hold_fc.time_index, hold_fc.values().ravel(), strict=False):
        target = pd.Timestamp(t)
        observado = target in evaluables and target in reales
        filas.append(
            {
                "table": table,
                "country": country,
                "category": category,
                "model": model,
                "origin": _origin_of(target).strftime("%Y-%m-%d"),
                "target": target.strftime("%Y-%m-%d"),
                "h": FORECAST_HORIZON,
                "y_pred": float(v),
                # ★ nulo, no ausente: la fila existe aunque el mes no sea evaluable
                "y_true": reales[target] if observado else None,
                "observed": bool(observado),
                "mase_scale": float(scale),
            }
        )
    return filas
