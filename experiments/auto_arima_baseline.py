"""Baseline Auto-ARIMA (selección de orden por AICc) bajo el MISMO walk-forward del pool.

Pre-empta el ataque de revisor "no incluiste un clásico afinado": el orden $(p,d,q)$ y el
término de tendencia (AI3: "c" si d=0, "t" si d=1) se seleccionan por AICc sobre las
observaciones F crudas previas al hold-out (sin fuga, sin meses interpolados) y se evalúa
con el protocolo idéntico (``walkforward.backtest``; "auto_arima" está en
``RETRAIN_EACH_STEP`` → reentrena cada mes como arima/sarima), así su MASE es comparable
fila a fila con ``campaign_pool``. statsforecast/pmdarima no compilan en py3.14/macOS,
así que la selección usa ``statsmodels`` (ya instalado).

Salida: reports/eval/auto_arima_baseline.csv (country,category,table,order,hold_mase) + un
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


def _trend_for(d: int) -> str:
    """AI3: trend spec matched to the differencing order — "c" (mean) for d=0,
    "t" (drift after one difference) for d=1. The previous trend="n" forced a
    zero-mean level (d=0) or zero drift (d=1), handicapping the baseline on
    series with a decades-long trend."""
    return "c" if d == 0 else "t"


def _select_order(y: np.ndarray) -> tuple[int, int, int]:
    """(p,d,q) minimizing in-sample AICc (leakage-free: pre-hold-out F obs only).

    The trend term enters the AICc selection with the same mapping used by the
    evaluated model (``_trend_for``), so selection and evaluation see the SAME
    model family (AI3).
    """
    best, best_aicc = (1, 1, 1), np.inf
    for p, d, q in GRID:
        if p == 0 and q == 0:
            continue
        try:
            res = SARIMAX(
                y, order=(p, d, q), trend=_trend_for(d), enforce_stationarity=False, enforce_invertibility=False
            ).fit(disp=0, maxiter=50)
            if np.isfinite(res.aicc) and res.aicc < best_aicc:
                best_aicc, best = res.aicc, (p, d, q)
        except Exception:  # noqa: BLE001 — orden inestable → se descarta
            continue
    return best


def run() -> Path:
    rows = []
    dropped: list[tuple[str, str, str, str]] = []  # (tabla, país, categoría, excepción): no convergió
    for table in config.TABLES:
        cat = dataset.list_series(table=table, block="family", countries=config.PILOT_COUNTRIES)
        for r in cat.itertuples():
            try:
                # AI3: order selection runs on the RAW F series (real observations
                # only). Selecting on the interpolated `to_timeseries` grid let the
                # continuity filler shape the AICc; the densified series is used
                # only to locate the protocol's hold-out cutoff.
                raw = dataset.load_series(r.country, r.category, table).astype("float64")
                ts = models.to_timeseries(raw)
                split = ts.time_index[-config.HOLDOUT]
                insample = raw[raw.index < split].to_numpy()
                order = _select_order(insample)
                trend = _trend_for(order[1])
                res = walkforward.backtest(
                    "auto_arima",
                    r.country,
                    r.category,
                    table,
                    model=ARIMA(p=order[0], d=order[1], q=order[2], trend=trend),
                )
                rows.append(
                    {
                        "country": r.country,
                        "category": r.category,
                        "table": table,
                        "order": f"{order}",
                        "trend": trend,
                        "hold_mase": round(res.holdout["mase"], 4),
                    }
                )
                log.info(
                    "%s/%s/%s order=%s trend=%s mase=%.3f",
                    table,
                    r.country,
                    r.category,
                    order,
                    trend,
                    res.holdout["mase"],
                )
            except Exception as e:  # noqa: BLE001 — orden inestable → se DECLARA, no se descarta en silencio
                dropped.append((table, r.country, r.category, type(e).__name__))
                log.warning(
                    "DROP %s/%s/%s (Auto-ARIMA no convergió): %s: %s",
                    table,
                    r.country,
                    r.category,
                    type(e).__name__,
                    e,
                )
    df = pd.DataFrame(rows)
    out = REPORTS / "eval" / "auto_arima_baseline.csv"
    df.to_csv(out, index=False)
    # FIX #21c: la media/mediana por tabla (y el mean que build_key_facts publica como
    # \factAutoarimaFadMean) se computan SOLO sobre las series que convergieron. Una serie
    # descartada —hoy india/F2A/FAD: el orden (3,1,3) que elige AICc revienta con LinAlgError
    # en la reinicialización estacionaria del filtro de Kalman al reajustarse en el walk-forward—
    # NO debe desaparecer en silencio (el 25→24). Aquí se DECLARA la n por tabla y qué series
    # faltan; el CSV escrito es idéntico (solo se añade el reporte).
    summary = df.groupby("table")["hold_mase"].agg(["median", "mean", "count"]).round(4)
    log.info("Auto-ARIMA hold MASE por tabla (count = series convergidas):\n%s", summary.to_string())
    if dropped:
        log.warning(
            "Auto-ARIMA descartó %d serie(s) por no convergencia: %s",
            len(dropped),
            ", ".join(f"{t}/{c}/{k}" for t, c, k, _ in dropped),
        )
    print("AUTO-ARIMA hold MASE por tabla (median/mean/count = series convergidas):")
    print(summary.to_string())
    if dropped:
        print("DESCARTADAS (no convergieron):", ", ".join(f"{t}/{c}/{k}" for t, c, k, _ in dropped))
    return out


if __name__ == "__main__":
    run()
