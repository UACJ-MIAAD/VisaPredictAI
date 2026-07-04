"""Intervalos de predicción 95% + CRPS para el GANADOR profundo global (cierra la dim. probabilística).

Reentrena el modelo profundo ganador (BiTCN/AutoBiTCN) GLOBAL + diferencia y produce, además
del punto, CUANTILES conformes leakage-free. Reintegra TODOS los cuantiles a nivel (Δ → nivel
con el último real, 1 paso) y guarda un CSV ancho que el entorno principal evalúa con cobertura
empírica, interval score (MSIS) y CRPS aproximado por cuantiles (``eval_deep_pi.py``).

Two mechanisms:
  * default — split-conformal nativo de neuralforecast (``PredictionIntervals``). AN3: the
    calibration window is ``--n-windows`` (default 36; the old n=10 imposed a STRUCTURAL
    coverage ceiling of n/(n+1) ~ 0.909 on the 97.5% conformal quantile, so the nominal
    0.95 was unreachable by construction), and ``--seeds`` (>= 3) re-runs the whole cv per
    seed and aggregates point + every quantile by the per-(series, month) median.
  * ``--cqr`` (AN5) — Conformalized Quantile Regression: the model trains with MQLoss on
    levels {80, 95} (asymmetric native quantiles — retrogressions get asymmetric bands the
    symmetric split cannot give), then each level's band is conformally adjusted per series
    with the first ``--cqr-calib`` (>= 36) cv windows: E_i = max(lo - y, y - hi), q_hat =
    finite-sample quantile (method='higher'), lo -= q_hat, hi += q_hat. Only the remaining
    HOLDOUT windows (post-calibration) are written out. Output: ``deep_pi_{table}_cqr.csv``.

Corre en ``ante_nf``. Salida: ``reports/eval/deep_pi_{table}[_cqr].csv`` (unique_id, ds, y,
<model>, <model>-lo-95, <model>-hi-95, ... para cada nivel).

Uso:  ante_nf/bin/python experiments/run_deep_pi.py --table DFF --model BiTCN --max-steps 800
      ante_nf/bin/python experiments/run_deep_pi.py --table FAD --model BiTCN --max-steps 800 --local-scaler
      ante_nf/bin/python experiments/run_deep_pi.py --table DFF --cqr --seeds 1,2,3
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from run_global_deep import HOLDOUT, load_panel

LEVELS = [50, 80, 90, 95]
CQR_LEVELS = [80, 95]


def _diff_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """First-difference the target per series (the global deep recipe trains on deltas)."""
    train = []
    for _uid, g in panel.groupby("unique_id"):
        g = g.sort_values("ds").copy()
        g["y"] = g["y"].diff()
        train.append(g.iloc[1:])
    return pd.concat(train, ignore_index=True)


def _qcols(cv: pd.DataFrame, model: str) -> list[str]:
    """Point + quantile columns of a cross_validation frame (handles '-median' from MQLoss)."""
    return [c for c in cv.columns if c in (model, f"{model}-median") or "-lo-" in c or "-hi-" in c]


def _aggregate_seeds(frames: list[pd.DataFrame], model: str) -> pd.DataFrame:
    """Median across seeds per (unique_id, ds) for the point and every quantile column (AN3)."""
    if len(frames) == 1:
        return frames[0]
    cat = pd.concat(frames, ignore_index=True)
    cols = _qcols(cat, model)
    agg = cat.groupby(["unique_id", "ds"], as_index=False).agg({"y": "first", **{c: "median" for c in cols}})
    return agg


def _cqr_adjust(cv: pd.DataFrame, model: str, n_calib: int) -> pd.DataFrame:
    """Per-series CQR adjustment (AN5): calibrate each level's conformity scores on the
    first ``n_calib`` cv windows, widen the remaining windows, drop the calibration rows.

    Works on the differenced scale; the reintegration to level is an additive shift per
    row, so adjusting before or after reintegration is equivalent.
    """
    out = []
    for _uid, g in cv.groupby("unique_id"):
        g = g.sort_values("ds").copy()
        if len(g) <= n_calib:
            continue  # series without post-calibration windows contributes nothing
        cal, ev = g.iloc[:n_calib], g.iloc[n_calib:].copy()
        for lvl in CQR_LEVELS:
            lo_c, hi_c = f"{model}-lo-{lvl}", f"{model}-hi-{lvl}"
            e = np.maximum(cal[lo_c] - cal["y"], cal["y"] - cal[hi_c]).to_numpy()
            n = len(e)
            alpha = 1 - lvl / 100.0
            q_level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
            q_hat = float(np.quantile(e, q_level, method="higher"))
            ev[lo_c] = ev[lo_c] - q_hat
            ev[hi_c] = ev[hi_c] + q_hat
        out.append(ev)
    return pd.concat(out, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="DFF")
    ap.add_argument("--block", default="family")
    ap.add_argument("--model", default="BiTCN")
    ap.add_argument("--max-steps", type=int, default=800)
    ap.add_argument("--local-scaler", action="store_true")
    ap.add_argument("--seeds", default="1,2,3", help="comma-separated seeds; median-aggregated (AN3)")
    ap.add_argument(
        "--n-windows", type=int, default=36, help="conformal calibration windows (AN3; 10 capped cov at n/(n+1))"
    )
    ap.add_argument("--cqr", action="store_true", help="MQLoss quantile head + CQR adjustment (AN5)")
    ap.add_argument("--cqr-calib", type=int, default=36, help="cv windows reserved to calibrate CQR (>=36)")
    args = ap.parse_args()
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    if args.cqr and args.cqr_calib < 36:
        ap.error("--cqr-calib must be >= 36 (finite-sample quantile at 95% needs it)")

    import torch

    torch.set_num_threads(1)
    from neuralforecast import NeuralForecast
    from neuralforecast.models import NHITS, BiTCN, PatchTST, TiDE
    from neuralforecast.utils import PredictionIntervals

    panel = load_panel(args.table, args.block)
    level_df = panel.copy()
    train = _diff_panel(panel)

    input_size = 36 if args.table == "FAD" else 18
    cls = {"BiTCN": BiTCN, "TiDE": TiDE, "NHITS": NHITS, "PatchTST": PatchTST}[args.model]

    frames: list[pd.DataFrame] = []
    for seed in seeds:
        c = dict(
            h=1,
            input_size=input_size,
            max_steps=args.max_steps,
            scaler_type="standard",
            random_seed=seed,
            enable_progress_bar=False,
            logger=False,
            enable_model_summary=False,
        )
        if args.cqr:
            # AN5: native asymmetric quantiles via MQLoss on levels {80, 95} (median + 4 bounds);
            # cv covers HOLDOUT + calibration windows so CQR can calibrate leakage-free.
            from neuralforecast.losses.pytorch import MQLoss

            c["loss"] = MQLoss(level=CQR_LEVELS)
            nf = NeuralForecast(
                models=[cls(**c)], freq="MS", local_scaler_type="standard" if args.local_scaler else None
            )
            cv = nf.cross_validation(df=train, n_windows=HOLDOUT + args.cqr_calib, step_size=1, refit=False)
        else:
            nf = NeuralForecast(
                models=[cls(**c)], freq="MS", local_scaler_type="standard" if args.local_scaler else None
            )
            # conformal: calibra residuales en ventanas del pasado de cada cutoff -> PI sin fuga.
            # neuralforecast exige refit=True con prediction_intervals (recalibra en cada cutoff).
            pi = PredictionIntervals(n_windows=args.n_windows, method="conformal_distribution", step_size=1)
            cv = nf.cross_validation(
                df=train, n_windows=HOLDOUT, step_size=1, refit=True, prediction_intervals=pi, level=LEVELS
            )
        frames.append(cv.reset_index())
        print(f"seed {seed}: cv {len(frames[-1])} rows")

    cv = _aggregate_seeds(frames, args.model)
    if args.cqr:
        cv = _cqr_adjust(cv, args.model, args.cqr_calib)
        # eval_deep_pi expects the point column under the plain model name.
        if f"{args.model}-median" in cv.columns:
            cv = cv.rename(columns={f"{args.model}-median": args.model})

    lvl = level_df.set_index(["unique_id", "ds"])["y"]
    prev_ds = cv["ds"] - pd.DateOffset(months=1)
    prev = np.array([lvl.get(k, np.nan) for k in zip(cv["unique_id"], prev_ds, strict=True)])
    # reintegrar punto y CADA cuantil: nivel = prev_real + Δ (monótono preserva el orden de cuantiles)
    qcols = _qcols(cv, args.model)
    for c2 in qcols:
        cv[c2] = prev + cv[c2].to_numpy()
    out = cv[["unique_id", "ds", "y", *qcols]].copy()
    # la `y` del cv es Δy (objetivo diferenciado); guardar el NIVEL real para evaluar.
    out["y"] = np.array([lvl.get(k, np.nan) for k in zip(out["unique_id"], out["ds"], strict=True)])
    suffix = "_cqr" if args.cqr else ""
    path = Path(__file__).resolve().parent.parent / "reports" / "eval" / f"deep_pi_{args.table}{suffix}.csv"
    out.to_csv(path, index=False)
    print(f"guardado {path.name} ({len(out)} filas, {len(seeds)} seeds, cols {qcols})")


if __name__ == "__main__":
    main()
