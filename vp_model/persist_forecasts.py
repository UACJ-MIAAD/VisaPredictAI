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
# AQ: naive1 joined the curated set — it won both MCS at h=1 and the champion
# deck now tracks it, so its holdout forecasts must be persisted (they are free).
# 'drift' (random walk with drift) added as a first-class challenger (7-jul-2026):
# it dominates on the ROLLING multi-horizon backtest but LOSES to naive1 on the fixed
# hold-out at h=1 (regime-dependent) — the champion-challenger must track that verdict.
CURATED = ("naive1", "theta", "ets", "sarima", "arima", "kalman", "catboost", "lightgbm", "drift")


def _rows(table: str, block: str, models_set: tuple[str, ...]) -> list[dict]:
    """Filas largas (model, country, category, date, actual, forecast) del hold-out F-only."""
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
    return rows


def run(table: str = "FAD", block: str = "family", models_set: tuple[str, ...] = CURATED) -> Path:
    out = REPORTS / "eval" / f"holdout_forecasts_{table}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_rows(table, block, models_set)).to_csv(out, index=False)
    log.info("escrito -> %s", out)
    return out


def persist_missing(table: str = "FAD", block: str = "family", models_set: tuple[str, ...] = CURATED) -> Path:
    """Idempotente: añade al CSV SOLO los modelos de ``models_set`` ausentes; los existentes
    quedan BYTE-idénticos (append). Para incorporar un modelo nuevo al pool sin re-derivar el
    resto (los GBMs pueden tener micro-drift de float al reentrenar → no se tocan)."""
    out = REPORTS / "eval" / f"holdout_forecasts_{table}.csv"
    if not out.exists():
        return run(table, block, models_set)
    have = set(pd.read_csv(out, usecols=["model"]).model.unique())
    missing = tuple(m for m in models_set if m not in have)
    if not missing:
        log.info("%s: nada que añadir (ya %s)", table, sorted(have))
        return out
    new = pd.DataFrame(_rows(table, block, missing))
    new["date"] = pd.to_datetime(new["date"]).dt.strftime("%Y-%m-%d")  # match del formato existente
    new.to_csv(out, mode="a", header=False, index=False)
    log.info("%s: añadidos %s (+%d filas)", table, missing, len(new))
    return out


if __name__ == "__main__":
    persist_missing("FAD")
    persist_missing("DFF")
