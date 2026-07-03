"""TabPFN-TS (foundation model como regresión tabular) — el top pick de la investigación.

TabPFN-TS (arXiv 2501.02945) es SOTA en pronóstico informado por covariables y fuerte en
series cortas + horizonte corto, zero-shot (sin entrenar por serie). Aquí se evalúa con
walk-forward de 1 paso sobre el hold-out de 24m, prediciendo el PANEL completo en cada paso
(aprovecha las series relacionadas en el contexto), y se compara contra el listón (FAD 0.117 /
DFF 0.090). Las features de calendario las genera el pipeline; se evalúa F-only, leakage-free.

⚠️ Requiere ``TABPFN_TOKEN`` (licencia gratuita: registro en https://ux.priorlabs.ai, aceptar
licencia, copiar API key). Correr en ``ante_tab``:
    TABPFN_TOKEN=... ante_tab/bin/python experiments/improve_tabpfn.py [--table FAD] [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
HOLDOUT = 24


def _panel(table: str) -> pd.DataFrame:
    import run_global_deep as R

    p = R.load_panel(table, "family")
    return p.rename(columns={"unique_id": "item_id", "ds": "timestamp", "y": "target"})


def _actuals(parquet: pd.DataFrame, country: str, category: str, table: str) -> pd.Series:
    """Serie F-only indexada por fecha — desde el parquet (ante_tab no tiene vp_model/duckdb)."""
    g = parquet[
        (parquet["country"] == country)
        & (parquet["category"] == category)
        & (parquet["table"] == table)
        & (parquet["status"] == "F")
    ]
    return g.set_index(pd.to_datetime(g["bulletin_date"]))["days_since_base"].astype("float64").sort_index()


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
    # CLIENT: usa la API de PriorLabs con TABPFN_TOKEN (los pesos LOCAL exigen aceptar licencia
    # en terminal interactiva). Datos públicos del Visa Bulletin.
    pipe = TabPFNTSPipeline(tabpfn_mode=TabPFNMode.CLIENT)

    preds = []  # (item_id, timestamp, forecast)
    for t in holdout_dates:  # walk-forward 1 paso: contexto = todo < t, predecir t (panel completo)
        ctx = panel[panel["timestamp"] < t]
        fut = panel[panel["timestamp"] == t][["item_id", "timestamp"]]
        if fut.empty:
            continue
        out = None
        for attempt in range(5):  # la API CLIENT da 409 por concurrencia; reintentar con backoff
            try:
                out = pipe.predict_df(ctx, fut).reset_index()
                break
            except Exception as e:  # noqa: BLE001
                if "409" not in str(e) or attempt == 4:
                    raise
                import time

                time.sleep(5 * (attempt + 1))
        if out is None:
            continue
        col = "target" if "target" in out.columns else [c for c in out.columns if "0.5" in str(c) or c == "mean"][0]
        for _, r in out.iterrows():
            preds.append({"item_id": r["item_id"], "timestamp": r["timestamp"], "forecast": float(r[col])})
    fc = pd.DataFrame(preds)
    fc.to_csv(ROOT / "reports" / f"tabpfn_forecasts_{args.table}.csv", index=False)  # no perder las predicciones

    # eval F-only por serie (la fecha del hold-out debe ser F real); actuals desde el parquet
    raw = pd.read_parquet(PANEL)
    mases = []
    for item, g in fc.groupby("item_id"):
        country, _block, category = item.split("/")
        full = _actuals(raw, country, category, args.table)
        if full.empty:
            continue
        g = g[g["timestamp"].isin(full.index)]  # F-only
        if g.empty:
            continue
        scale = _naive_scale(full[full.index < g["timestamp"].min()].to_numpy())
        y = full.reindex(g["timestamp"]).to_numpy()
        mases.append(float(np.mean(np.abs(y - g["forecast"].to_numpy()))) / scale)
    mase = float(np.mean(mases))
    import json

    kf = json.loads(Path("reports/key_facts.json").read_text())
    liston = kf["fad_champion_mase"] if args.table == "FAD" else kf["bitcn_dff_mean"]
    print(f"\n=== TabPFN-TS {args.table} ({len(mases)} series) ===")
    print(f"  MASE {mase:.4f}  vs listón {liston}  -> {'MEJORA' if mase < liston else 'no mejora'}")
    if args.mlflow:
        from vp_data import tracking

        tracking.log_run(
            f"improve_{args.table}",
            f"tabpfn_ts/{args.table}",
            params={"method": "tabpfn_ts", "table": args.table, "layer": "improve"},
            metrics={"hold_mase": mase},
            tags={"layer": "improve", "technique": "tabpfn_ts"},
        )


if __name__ == "__main__":
    main()
