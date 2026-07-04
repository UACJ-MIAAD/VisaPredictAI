"""Global simplex-constrained stacking of the persisted pool forecasts (AM2).

Replaces the old per-series LassoCV stacking, which lost by construction:
  * ~10 training points vs 6 regressors PER SERIES -> 0.238/0.304 hold MASE (2-3.5x the
    champion) — a degenerate configuration, not evidence against stacking;
  * its "combinacion CONVEXA" comment was FALSE: ``positive=True`` + no intercept do NOT
    impose sum-to-1, so the fitted weights could shrink the level itself;
  * its baseline ("best base model on the SAME test half") was an ORACLE, not a baseline:
    it picked the winner by looking at the test labels (documented here so nobody quotes
    those numbers as a fair comparison again).

This version fits ONE weight vector per table on the simplex (w >= 0, sum(w) = 1 — the
M4-winner style combination) by least squares on per-series SCALED errors: each series'
rows are divided by its naive scale so no long/large series dominates the objective.

Training region: the leakage-free stretch BEFORE the test rows. The AQ campaign will
persist SELECTION-region forecasts as ``reports/eval/selection_forecasts_{table}.csv``;
when that file exists it is used to fit and the FULL persisted hold-out is the test. Until
then the script falls back to calibrating on the FIRST HALF of the hold-out and testing on
the second half (leakage-free w.r.t. the test rows, but the number is provisional — NOT
canonical, and NOT comparable with full-hold-out figures).

Evaluation: ``metrics.mase_by_series`` (canonical F-only scorer, AM4d) over deduplicated
replica representatives (AM4b). Fair baseline: the base model with the best mean
SELECTION-region MASE (``model_comparison_{table}21.csv``), scored on the same test rows.

Corre en ``ante``. Uso:  ante/bin/python experiments/improve_stacking.py [--mlflow]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from vp_data import tracking
from vp_model import dataset, ensemble
from vp_model.metrics import mase_by_series, naive_scale_before

REPORTS = Path(__file__).resolve().parent.parent / "reports"


def fit_simplex_weights(f: np.ndarray, y: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """Least squares on the simplex: min ||(y - F @ w) / scale||^2 s.t. w >= 0, sum(w) = 1.

    ``f`` is (n_rows, n_models), ``y`` (n_rows,), ``scale`` (n_rows,) the per-row naive
    scale of the row's series (so every series contributes in MASE units).
    """
    a = f / scale[:, None]
    b = y / scale
    norm = float(np.mean(np.abs(b))) or 1.0  # conditioning only: SLSQP stalls on huge gradients
    a, b = a / norm, b / norm
    n_models = f.shape[1]
    x0 = np.full(n_models, 1.0 / n_models)
    res = minimize(
        lambda w: float(np.mean((b - a @ w) ** 2)),
        x0,
        jac=lambda w: (2.0 / len(b)) * (a.T @ (a @ w - b)),
        bounds=[(0.0, 1.0)] * n_models,
        constraints=[{"type": "eq", "fun": lambda w: float(np.sum(w) - 1.0)}],
        method="SLSQP",
        # scaled residuals make the objective ~1e-5; the default ftol=1e-6 would declare
        # convergence at the uniform starting point without taking a single step.
        options={"maxiter": 500, "ftol": 1e-12},
    )
    w = np.clip(res.x, 0.0, None)
    return w / w.sum()


def _split_frames(table: str) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """(train frame, test frame, provenance note) — long frames with model/forecast/actual.

    Prefers the AQ selection-region artifact; falls back to the first/second half of the
    persisted hold-out (per series, by date order) with an explicit provisional note.
    """
    hold = pd.read_csv(REPORTS / "eval" / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    sel_path = REPORTS / "eval" / f"selection_forecasts_{table}.csv"
    if sel_path.exists():
        return pd.read_csv(sel_path, parse_dates=["date"]), hold, "train=selection region (AQ), test=full hold-out"
    cut = hold.groupby(["country", "category"])["date"].transform(lambda d: d.quantile(0.5))
    return (
        hold[hold.date <= cut],
        hold[hold.date > cut],
        "PROVISIONAL: train=1st half hold-out (no selection_forecasts CSV yet; AQ will persist it)",
    )


def _training_arrays(train: pd.DataFrame, base: list[str], table: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stack per-series rows (forecast matrix, actuals, per-row naive scale), F-masked."""
    wide = train.pivot_table(index=["country", "category", "date"], columns="model", values="forecast")
    act = train.pivot_table(index=["country", "category", "date"], columns="model", values="actual").mean(axis=1)
    fs, ys, ss = [], [], []
    for (country, category), g in wide.groupby(level=[0, 1]):
        g = g[base].dropna()
        if g.empty:
            continue
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        dates = g.index.get_level_values("date")
        fmask = dates.isin(full.index)  # B1: fit only on real F observations
        if not fmask.any():
            continue
        scale = naive_scale_before(full, dates.min())
        if not np.isfinite(scale):
            continue
        fs.append(g.to_numpy()[fmask])
        ys.append(act.loc[g.index].to_numpy()[fmask])
        ss.append(np.full(int(fmask.sum()), scale))
    return np.vstack(fs), np.concatenate(ys), np.concatenate(ss)


def _evaluate(table: str) -> dict:
    train, test, note = _split_frames(table)
    base = sorted(set(train.model.unique()) & set(test.model.unique()))
    f, y, s = _training_arrays(train, base, table)
    w = fit_simplex_weights(f, y, s)

    # test predictions: F_test @ w per series×date -> canonical scorer + dedup denominator
    wide_t = test.pivot_table(index=["country", "category", "date"], columns="model", values="forecast")
    act_t = test.pivot_table(index=["country", "category", "date"], columns="model", values="actual").mean(axis=1)
    wt = wide_t[base].dropna()
    comb = wt.reset_index()[["country", "category", "date"]]
    comb["pred"] = wt.to_numpy() @ w
    comb["actual"] = act_t.loc[wt.index].to_numpy()
    comb, _n_raw, n_eff = ensemble.representative_filter(comb, table, test)
    stack = mase_by_series(comb, table, pred_col="pred", actual_col="actual")

    # fair baseline: best base by mean SELECTION-region MASE (leakage-free), same test rows
    mc = pd.read_csv(REPORTS / "eval" / f"model_comparison_{table}21.csv").pipe(lambda d: d[d.run_id == d.run_id.max()])
    sel_means = mc[mc.model.isin(base)].groupby("model")["sel_mase"].mean()
    best_sel = str(sel_means.idxmin())
    bcomb = comb[["country", "category", "date", "actual"]].copy()
    idx = pd.MultiIndex.from_frame(comb[["country", "category", "date"]])
    bcomb["pred"] = wt.loc[idx, best_sel].to_numpy()
    baseline = mase_by_series(bcomb, table, pred_col="pred", actual_col="actual")

    return {
        "weights": dict(zip(base, np.round(w, 4), strict=True)),
        "stack": float(stack.mean()),
        "baseline": float(baseline.mean()),
        "best_sel": best_sel,
        "n_eff": n_eff,
        "note": note,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    for table in ("FAD", "DFF"):
        r = _evaluate(table)
        print(f"\n=== STACKING SIMPLEX GLOBAL {table} ({r['n_eff']} series efectivas) ===")
        print(f"  {r['note']}")
        print(f"  pesos simplex  : {r['weights']}")
        print(f"  stacking       : MASE {r['stack']:.4f}")
        print(f"  mejor base sel ({r['best_sel']}): MASE {r['baseline']:.4f}")
        verdict = "MEJORA" if r["stack"] < r["baseline"] else "no mejora"
        print(
            f"  -> {verdict} ({(r['baseline'] - r['stack']) / r['baseline'] * 100:+.1f}% vs mejor base por selección)"
        )
        if args.mlflow:
            tracking.log_run(
                f"improve_{table}",
                f"stacking_simplex/{table}",
                params={"method": "stacking_simplex", "table": table, "layer": "improve", "note": r["note"]},
                metrics={"hold_mase": r["stack"], "baseline_sel_mase": r["baseline"]},
                tags={"layer": "improve", "technique": "simplex_stacking"},
            )


if __name__ == "__main__":
    main()
