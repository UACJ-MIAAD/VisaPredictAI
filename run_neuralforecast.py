"""Entrenamiento GLOBAL de transformers/MLP modernos vía neuralforecast (Nixtla).

Corre en el venv AISLADO ``ante_nf`` (neuralforecast exige pandas<3, incompatible con
el pandas 3.0.0 del pipeline principal). Es además la implementación de la estrategia
GLOBAL (Montero-Manso & Hyndman 2021): un solo modelo aprende sobre las 25 series del
panel apiladas, de modo que las series cortas piden prestada señal al resto.

Salida: ``reports/neuralforecast_forecasts.csv`` (unique_id, ds, modelo, pronóstico a
1 paso sobre el hold-out de 24 meses), que el entorno principal evalúa con las MISMAS
métricas (MASE/CRPS/...) para comparar de forma justa contra el pool local.

Uso:  ante_nf/bin/python run_neuralforecast.py [--table FAD] [--fast]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
OUT = ROOT / "reports" / "neuralforecast_forecasts.csv"
PILOT = ("mexico", "india", "china", "philippines", "all_chargeability")
HOLDOUT = 24


def load_panel(table: str, block: str) -> pd.DataFrame:
    """Panel en formato largo de neuralforecast (unique_id, ds, y), F-only y mensual."""
    df = pd.read_parquet(PANEL)
    df = df[
        (df["table"] == table) & (df["block"] == block) & (df["status"] == "F") & (df["country"].isin(PILOT))
    ].copy()
    df["unique_id"] = df["country"] + "/" + df["category"]
    df["ds"] = pd.to_datetime(df["bulletin_date"])
    out = []
    for uid, g in df[["unique_id", "ds", "days_since_base"]].groupby("unique_id"):
        s = g.set_index("ds")["days_since_base"].sort_index()
        s = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="MS")).interpolate()
        out.append(pd.DataFrame({"unique_id": uid, "ds": s.index, "y": s.to_numpy()}))
    return pd.concat(out, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="FAD")
    ap.add_argument("--block", default="family")
    ap.add_argument("--fast", action="store_true", help="pocas épocas para smoke test")
    args = ap.parse_args()

    from neuralforecast import NeuralForecast
    from neuralforecast.models import NHITS, PatchTST, iTransformer

    panel = load_panel(args.table, args.block)
    n_uid = panel["unique_id"].nunique()
    print(f"panel: {n_uid} series, {len(panel)} filas ({args.table}/{args.block})")

    epochs = 5 if args.fast else 80
    L = 24  # ventana de entrada
    common = dict(h=1, input_size=L, max_steps=epochs, scaler_type="standard", enable_progress_bar=False)
    models = [
        PatchTST(**common),
        iTransformer(**common, n_series=n_uid),
        NHITS(**common),
    ]
    nf = NeuralForecast(models=models, freq="MS")
    # cross_validation = walk-forward GLOBAL: 1 paso, 24 ventanas (= nuestro hold-out).
    cv = nf.cross_validation(df=panel, n_windows=HOLDOUT, step_size=1, refit=False)
    cv = cv.reset_index()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cv.to_csv(OUT, index=False)
    print(f"guardado {OUT.relative_to(ROOT)} ({len(cv)} filas, modelos: {[m.__class__.__name__ for m in models]})")


if __name__ == "__main__":
    main()
