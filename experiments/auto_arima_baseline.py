"""Baseline Auto-ARIMA (selección de orden por AICc) bajo el MISMO walk-forward del pool.

Pre-empta el ataque de revisor "no incluiste un clásico afinado": el orden $(p,d,q)$ se
selecciona por AICc sobre la ventana previa al hold-out (sin fuga) y se evalúa con el
protocolo idéntico (``walkforward.backtest``), así su MASE es comparable fila a fila con
``campaign_pool``. statsforecast/pmdarima no compilan en py3.14/macOS, así que la
selección usa ``statsmodels`` (ya instalado).

Salida: reports/auto_arima_baseline.csv (country,category,table,order,hold_mase) + un
resumen (mediana por tabla) para citar en el paper junto a ETS/Theta.

Uso (ante):  ante/bin/python experiments/auto_arima_baseline.py
"""

from __future__ import annotations

import warnings
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
from darts.models import ARIMA
from statsmodels.tsa.statespace.sarimax import SARIMAX

from vp_model import config, dataset, models, walkforward

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
log = config.get_logger("auto_arima")
GRID = list(product(range(0, 4), range(0, 2), range(0, 4)))  # (p,d,q): 32 candidatos


def _select_order(y: np.ndarray) -> tuple[int, int, int]:
    """Orden (p,d,q) que minimiza AICc en-muestra (leakage-free: solo el pre-hold-out)."""
    best, best_aicc = (1, 1, 1), np.inf
    for p, d, q in GRID:
        if p == 0 and q == 0:
            continue
        try:
            res = SARIMAX(y, order=(p, d, q), trend="n", enforce_stationarity=False, enforce_invertibility=False).fit(
                disp=0, maxiter=50
            )
            if np.isfinite(res.aicc) and res.aicc < best_aicc:
                best_aicc, best = res.aicc, (p, d, q)
        except Exception:  # noqa: BLE001 — orden inestable → se descarta
            continue
    return best


def run() -> Path:
    rows = []
    for table in config.TABLES:
        cat = dataset.list_series(table=table, block="family", countries=config.PILOT_COUNTRIES)
        for r in cat.itertuples():
            try:
                ts = models.to_timeseries(dataset.load_series(r.country, r.category, table))
                insample = ts[: -config.HOLDOUT].values().flatten()
                order = _select_order(insample)
                res = walkforward.backtest(
                    "auto_arima", r.country, r.category, table, model=ARIMA(p=order[0], d=order[1], q=order[2])
                )
                rows.append(
                    {
                        "country": r.country,
                        "category": r.category,
                        "table": table,
                        "order": f"{order}",
                        "hold_mase": round(res.holdout["mase"], 4),
                    }
                )
                log.info("%s/%s/%s order=%s mase=%.3f", table, r.country, r.category, order, res.holdout["mase"])
            except Exception as e:  # noqa: BLE001
                log.info("skip %s/%s/%s: %s", table, r.country, r.category, e)
    df = pd.DataFrame(rows)
    out = REPORTS / "auto_arima_baseline.csv"
    df.to_csv(out, index=False)
    summary = df.groupby("table")["hold_mase"].median().round(4).to_dict()
    log.info("Auto-ARIMA mediana hold MASE por tabla: %s (n=%d)", summary, len(df))
    print("AUTO-ARIMA median hold MASE:", summary, "| n =", len(df))
    return out


if __name__ == "__main__":
    run()
