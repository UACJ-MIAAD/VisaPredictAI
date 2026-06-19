"""Stacking lasso de pronósticos heterogéneos (global deep + clásicos), leakage-free.

Investigación (M4-weekly): el stacking vía lasso de modelos heterogéneos (RNN global + Theta +
TBATS + DHR-ARIMA) batió a todos los benchmarks. Aquí se aplica a nuestros finalistas
(AutoBiTCN/BiTCN global + Theta/ETS/SARIMA/CatBoost) sobre ``reports/finalist_forecasts_{table}.csv``.

Leakage-free: por serie, el hold-out de 24m se parte en CALIBRACIÓN (primera mitad, aprende los
pesos lasso) y TEST (segunda mitad, evalúa). El baseline justo = el mejor modelo base sobre el
MISMO tramo de test. Se reporta MASE de test y se loguea a MLflow vía tracking.

Corre en ``ante``. Uso:  ante/bin/python improve_stacking.py [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LassoCV

import tracking
from vp_model import dataset
from vp_model.metrics import naive_scale_before

REPORTS = Path(__file__).resolve().parent / "reports"
BASE = ("AutoBiTCN", "BiTCN", "theta", "ets", "sarima", "catboost")


def _evaluate(table: str) -> dict:
    fc = pd.read_csv(REPORTS / f"finalist_forecasts_{table}.csv", parse_dates=["date"])
    wide = fc.pivot_table(index=["country", "category", "date"], columns="model", values="forecast")
    actual = fc.pivot_table(index=["country", "category", "date"], columns="model", values="actual").iloc[:, 0]
    base = [b for b in BASE if b in wide.columns]
    stack_mase, best_base_mase = [], []
    for (country, category), idx in wide.groupby(level=[0, 1]).groups.items():
        g = wide.loc[idx, base].dropna()
        if len(g) < 8:  # hace falta calibración + test mínimos
            continue
        y = actual.loc[g.index].to_numpy()
        dates = g.index.get_level_values("date")
        cut = len(g) // 2
        Xc, yc, Xt, yt = g.iloc[:cut].to_numpy(), y[:cut], g.iloc[cut:].to_numpy(), y[cut:]
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        scale = naive_scale_before(full, dates[cut])  # escala con el pasado del test
        # combinación CONVEXA de forecasts (pesos>=0, SIN intercepto): el intercepto sobre
        # niveles tendenciales no transfiere del tramo de calibración al de test.
        lasso = LassoCV(positive=True, fit_intercept=False, cv=3, max_iter=5000).fit(Xc, yc)
        stack_mase.append(np.mean(np.abs(yt - lasso.predict(Xt))) / scale)
        # mejor modelo base sobre el MISMO test (baseline justo)
        best_base_mase.append(min(np.mean(np.abs(yt - Xt[:, j])) / scale for j in range(len(base))))
    return {
        "stack": float(np.mean(stack_mase)),
        "best_base": float(np.mean(best_base_mase)),
        "n_series": len(stack_mase),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    for table in ("FAD", "DFF"):
        r = _evaluate(table)
        print(f"\n=== STACKING LASSO {table} ({r['n_series']} series, test=2ª mitad del hold-out) ===")
        print(f"  stacking lasso : MASE {r['stack']:.4f}")
        print(f"  mejor base     : MASE {r['best_base']:.4f}")
        verdict = "MEJORA" if r["stack"] < r["best_base"] else "no mejora"
        print(f"  -> {verdict} ({(r['best_base'] - r['stack']) / r['best_base'] * 100:+.1f}% vs mejor base)")
        if args.mlflow:
            tracking.log_run(
                f"improve_{table}",
                f"stacking_lasso/{table}",
                params={"method": "stacking_lasso", "table": table, "layer": "improve"},
                metrics={"hold_mase": r["stack"], "best_base_mase": r["best_base"]},
                tags={"layer": "improve", "technique": "lasso_stacking"},
            )


if __name__ == "__main__":
    main()
