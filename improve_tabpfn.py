"""TabPFN-TS (foundation model como regresión tabular) — el top pick de la investigación.

TabPFN-TS (arXiv 2501.02945) es SOTA en pronóstico informado por covariables y fuerte en
series cortas + horizonte corto, zero-shot (sin entrenar por serie). Aquí se evalúa con
walk-forward de 1 paso sobre el hold-out de 24m, prediciendo el PANEL completo en cada paso
(aprovecha las series relacionadas en el contexto), y se compara contra el listón (FAD 0.117 /
DFF 0.090). Las features de calendario las genera el pipeline; se evalúa F-only, leakage-free.

⚠️ Requiere ``TABPFN_TOKEN`` (licencia gratuita: registro en https://ux.priorlabs.ai, aceptar
licencia, copiar API key). Correr en ``ante_tab``:
    TABPFN_TOKEN=... ante_tab/bin/python improve_tabpfn.py [--table FAD] [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
HOLDOUT = 24


def _panel(table: str) -> pd.DataFrame:
    import run_global_deep as R

    p = R.load_panel(table, "family")
    return p.rename(columns={"unique_id": "item_id", "ds": "timestamp", "y": "target"})


def _naive_scale(values: np.ndarray, m: int = 12) -> float:
    v = np.asarray(values, dtype="float64")
    d = np.abs(v[m:] - v[:-m]) if len(v) > m else np.abs(np.diff(v))
    s = float(np.mean(d)) if len(d) else 0.0
    return s if np.isfinite(s) and s > 0 else 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    from tabpfn_time_series import TabPFNMode, TabPFNTSPipeline

    panel = _panel(args.table)
    dates = sorted(panel["timestamp"].unique())
    holdout_dates = dates[-HOLDOUT:]
    pipe = TabPFNTSPipeline(tabpfn_mode=TabPFNMode.LOCAL, tabpfn_model_config={"device": "cpu"})

    preds = []  # (item_id, timestamp, forecast)
    for t in holdout_dates:  # walk-forward 1 paso: contexto = todo < t, predecir t (panel completo)
        ctx = panel[panel["timestamp"] < t]
        fut = panel[panel["timestamp"] == t][["item_id", "timestamp"]]
        if fut.empty:
            continue
        out = pipe.predict_df(ctx, fut).reset_index()
        col = "target" if "target" in out.columns else [c for c in out.columns if "0.5" in str(c) or c == "mean"][0]
        for _, r in out.iterrows():
            preds.append({"item_id": r["item_id"], "timestamp": r["timestamp"], "forecast": float(r[col])})
    fc = pd.DataFrame(preds)

    # eval F-only por serie (la fecha del hold-out debe ser F real en el panel original)
    from vp_model import dataset

    mases = []
    for item, g in fc.groupby("item_id"):
        country, _block, category = item.split("/")
        try:
            full = dataset.load_series(country, category, args.table).astype("float64")
        except KeyError:
            continue
        g = g[g["timestamp"].isin(full.index)]  # F-only
        if g.empty:
            continue
        scale = _naive_scale(full[full.index < g["timestamp"].min()].to_numpy())
        y = full.reindex(g["timestamp"]).to_numpy()
        mases.append(float(np.mean(np.abs(y - g["forecast"].to_numpy()))) / scale)
    mase = float(np.mean(mases))
    liston = 0.117 if args.table == "FAD" else 0.090
    print(f"\n=== TabPFN-TS {args.table} ({len(mases)} series) ===")
    print(f"  MASE {mase:.4f}  vs listón {liston}  -> {'MEJORA' if mase < liston else 'no mejora'}")
    if args.mlflow:
        import tracking

        tracking.log_run(
            f"improve_{args.table}",
            f"tabpfn_ts/{args.table}",
            params={"method": "tabpfn_ts", "table": args.table, "layer": "improve"},
            metrics={"hold_mase": mase},
            tags={"layer": "improve", "technique": "tabpfn_ts"},
        )


if __name__ == "__main__":
    main()
