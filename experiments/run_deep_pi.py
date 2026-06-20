"""Intervalos de predicción 95% + CRPS para el GANADOR profundo global (cierra la dim. probabilística).

Reentrena el modelo profundo ganador (BiTCN/AutoBiTCN) GLOBAL + diferencia y produce, además
del punto, CUANTILES conformes (split-conformal nativo de neuralforecast, leakage-free: se
calibran sobre el tramo de entrenamiento que precede a cada cutoff). Reintegra TODOS los
cuantiles a nivel (Δ → nivel con el último real, 1 paso) y guarda un CSV ancho que el entorno
principal evalúa con cobertura empírica, interval score (MSIS) y CRPS aproximado por cuantiles.

Corre en ``ante_nf``. Salida: ``reports/deep_pi_{table}.csv`` (unique_id, ds, y, <model>,
<model>-lo-95, <model>-hi-95, ... para cada nivel).

Uso:  ante_nf/bin/python experiments/run_deep_pi.py --table DFF --model BiTCN --max-steps 800
      ante_nf/bin/python experiments/run_deep_pi.py --table FAD --model BiTCN --max-steps 800 --local-scaler
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from run_global_deep import HOLDOUT, load_panel

LEVELS = [50, 80, 90, 95]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="DFF")
    ap.add_argument("--block", default="family")
    ap.add_argument("--model", default="BiTCN")
    ap.add_argument("--max-steps", type=int, default=800)
    ap.add_argument("--local-scaler", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    import torch

    torch.set_num_threads(1)
    from neuralforecast import NeuralForecast
    from neuralforecast.models import NHITS, BiTCN, PatchTST, TiDE
    from neuralforecast.utils import PredictionIntervals

    panel = load_panel(args.table, args.block)
    level_df = panel.copy()
    train = []
    for _uid, g in panel.groupby("unique_id"):
        g = g.sort_values("ds").copy()
        g["y"] = g["y"].diff()
        train.append(g.iloc[1:])
    train = pd.concat(train, ignore_index=True)

    input_size = 36 if args.table == "FAD" else 18
    c = dict(
        h=1,
        input_size=input_size,
        max_steps=args.max_steps,
        scaler_type="standard",
        random_seed=args.seed,
        enable_progress_bar=False,
        enable_model_summary=False,
    )
    cls = {"BiTCN": BiTCN, "TiDE": TiDE, "NHITS": NHITS, "PatchTST": PatchTST}[args.model]
    nf = NeuralForecast(models=[cls(**c)], freq="MS", local_scaler_type="standard" if args.local_scaler else None)
    # conformal: calibra residuales en ventanas del pasado de cada cutoff -> PI sin fuga.
    # neuralforecast exige refit=True con prediction_intervals (recalibra en cada cutoff).
    pi = PredictionIntervals(n_windows=10, method="conformal_distribution", step_size=1)
    cv = nf.cross_validation(
        df=train, n_windows=HOLDOUT, step_size=1, refit=True, prediction_intervals=pi, level=LEVELS
    ).reset_index()

    lvl = level_df.set_index(["unique_id", "ds"])["y"]
    prev_ds = cv["ds"] - pd.DateOffset(months=1)
    prev = np.array([lvl.get(k, np.nan) for k in zip(cv["unique_id"], prev_ds, strict=True)])
    # reintegrar punto y CADA cuantil: nivel = prev_real + Δ (monótono preserva el orden de cuantiles)
    qcols = [c2 for c2 in cv.columns if c2 == args.model or "-lo-" in c2 or "-hi-" in c2]
    for c2 in qcols:
        cv[c2] = prev + cv[c2].to_numpy()
    out = cv[["unique_id", "ds", "y", *qcols]].copy()
    # la `y` del cv es Δy (objetivo diferenciado); guardar el NIVEL real para evaluar.
    out["y"] = np.array([lvl.get(k, np.nan) for k in zip(out["unique_id"], out["ds"], strict=True)])
    path = Path(__file__).resolve().parent.parent / "reports" / f"deep_pi_{args.table}.csv"
    out.to_csv(path, index=False)
    print(f"guardado {path.name} ({len(out)} filas, cols {qcols})")


if __name__ == "__main__":
    main()
