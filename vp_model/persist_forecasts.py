"""Persiste los pronósticos de hold-out de un conjunto curado, para ensambles (US-N1).

La selección por serie no superó al mejor global (ver ``ensemble.py``); la combinación
de pronósticos es un mecanismo distinto (reduce varianza) y la sabiduría de las
M-competencias dice que la media simple suele ser robusta. Eso exige los pronósticos,
no solo las métricas, así que aquí se corre el walk-forward del set curado y se guardan
los pronósticos de los últimos 24 meses (hold-out) en formato largo:
``reports/holdout_forecasts_{table}.csv`` con (model, country, category, date, actual,
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
            af = actual.values().flatten()
            ff = hold_fc.slice_intersect(actual).values().flatten()
            rows += [
                {"model": m, "country": r.country, "category": r.category, "date": d, "actual": a, "forecast": f}
                for d, a, f in zip(dates, af, ff, strict=True)
            ]
        log.info("hold-out forecasts: %s/%s listo", r.country, r.category)
    out = REPORTS / f"holdout_forecasts_{table}.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    log.info("escrito -> %s (%d filas)", out, len(rows))
    return out


if __name__ == "__main__":
    run("FAD")
    run("DFF")
