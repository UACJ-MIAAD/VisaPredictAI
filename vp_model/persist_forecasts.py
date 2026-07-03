"""Persiste los pronósticos de hold-out de un conjunto curado, para ensambles (US-N1).

La selección por serie no superó al mejor global (ver ``ensemble.py``); la combinación
de pronósticos es un mecanismo distinto (reduce varianza) y la sabiduría de las
M-competencias dice que la media simple suele ser robusta. Eso exige los pronósticos,
no solo las métricas, así que aquí se corre el walk-forward del set curado y se guardan
los pronósticos de los últimos 24 meses (hold-out) en formato largo:
``reports/eval/holdout_forecasts_{table}.csv`` con (model, country, category, date, actual,
forecast). El conjunto curado son los mejores puntuales (estadísticos + GBMs ganadores).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from vp_model import dataset, walkforward
from vp_model.config import HOLDOUT, get_logger

log = get_logger("persist_forecasts")
REPORTS = Path(__file__).resolve().parent.parent / "reports"
CURATED = ("theta", "ets", "sarima", "arima", "kalman", "catboost", "lightgbm")


def run(table: str = "FAD", block: str = "family", models_set: tuple[str, ...] = CURATED) -> Path:
    cat = dataset.list_series(table=table, block=block)
    rows = []
    for r in cat.itertuples():
        # B1: persistir SOLO fechas con observación F real. El `actual` de los meses
        # interpolados por `to_timeseries` no es verdad publicada y contaminaba a los
        # consumidores (ensembles, DM, champion) que puntúan contra esta columna.
        fdates = dataset.load_series(r.country, r.category, table).index
        for m in models_set:
            try:
                ts, fc = walkforward.run_forecasts(m, r.country, r.category, table)
            except Exception as e:  # noqa: BLE001 — serie/modelo que falle no aborta el resto
                log.warning("skip %s %s/%s: %s", m, r.country, r.category, e)
                continue
            split = ts.time_index[-HOLDOUT]
            hold_fc = fc.split_before(split)[1]
            actual = ts.slice_intersect(hold_fc)
            dates = actual.time_index
            fmask = dates.isin(fdates)
            af = actual.values().flatten()[fmask]
            ff = hold_fc.slice_intersect(actual).values().flatten()[fmask]
            rows += [
                {"model": m, "country": r.country, "category": r.category, "date": d, "actual": a, "forecast": f}
                for d, a, f in zip(dates[fmask], af, ff, strict=True)
            ]
        log.info("hold-out forecasts: %s/%s listo", r.country, r.category)
    out = REPORTS / "eval" / f"holdout_forecasts_{table}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    log.info("escrito -> %s (%d filas)", out, len(rows))
    return out


if __name__ == "__main__":
    run("FAD")
    run("DFF")
