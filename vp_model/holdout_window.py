"""La ventana del hold-out que dibujan las figuras: la del PROTOCOLO, no un literal (G7 · puerta 2).

`make_result_figures` (F5) y `make_hero_figures` sombreaban 2024-08→2026-07, la ventana de una añada vieja del
panel. El protocolo de evaluación reserva los últimos ``config.HOLDOUT`` meses de cada serie, y en el panel
vigente esos meses terminan en el último boletín. Aquí la ventana se deriva de esas dos cosas, y una figura sólo
la usa si los pronósticos que va a dibujar la cubren EXACTAMENTE: unos pronósticos de otra añada no se dibujan
bajo la ventana de ésta.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from vp_model.artifact_receipt import PANEL_REL
from vp_model.config import HOLDOUT

RAIZ = Path(__file__).resolve().parent.parent


def panel_end(path: Path | None = None) -> pd.Timestamp:
    """El último boletín del panel, como primer día de su mes."""
    fechas = pd.to_datetime(pd.read_csv(path or RAIZ / PANEL_REL, usecols=["bulletin_date"])["bulletin_date"])
    return pd.Timestamp(fechas.max().to_period("M").to_timestamp())


def window(holdout: int = HOLDOUT) -> tuple[pd.Timestamp, pd.Timestamp]:
    """``(primer mes, último mes)`` del hold-out del protocolo: ``holdout`` meses que terminan en el panel."""
    fin = panel_end()
    return pd.Timestamp(fin - pd.DateOffset(months=holdout - 1)), fin


def window_of(forecasts: pd.DataFrame, holdout: int = HOLDOUT) -> tuple[pd.Timestamp, pd.Timestamp]:
    """La ventana del protocolo, SÓLO si cada serie de ``forecasts`` (``unique_id``, ``ds``) la cubre exactamente."""
    inicio, fin = window(holdout)
    esperado = pd.date_range(inicio, fin, freq="MS")
    if forecasts.empty:
        raise ValueError(f"no hay pronósticos que dibujar en la ventana {inicio:%Y-%m}→{fin:%Y-%m}")
    for uid, g in forecasts.groupby("unique_id"):
        ds = pd.DatetimeIndex(pd.to_datetime(g["ds"])).sort_values()
        if not ds.equals(esperado):
            raise ValueError(
                f"{uid}: los pronósticos cubren {ds.min():%Y-%m}→{ds.max():%Y-%m} ({len(ds)} meses) y la ventana del "
                f"protocolo es {inicio:%Y-%m}→{fin:%Y-%m}: son de otra añada del panel"
            )
    return inicio, fin
