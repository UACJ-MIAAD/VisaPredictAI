"""AL2 — GLOBAL gradient-boosting on the stacked panel (M5 recipe).

The local pool trains one model per series; the M5-winning recipe instead pools
ALL series into one table and lets a single GBM learn cross-series structure
through static identifiers. This is the #1 candidate to dethrone ETS/Theta.

Design (runs in the main ``ante`` venv — vp_model available):

  * target: Δy (first difference of the densified level, per series), causally
    reintegrated onto the last known level, exactly like ``models.Differenced``;
  * features (all computed from information available at the forecast origin,
    i.e. through month t-1, except calendar which is deterministic for t):
      - Δy lags {1, 2, 3, 6, 12, 24};
      - cyclic calendar of the TARGET month (month/fiscal sin+cos; ``year`` is
        deliberately dropped: the documented smell in config.COVARIATE_COLS is
        kept there only for provenance of the LOCAL trees — a NEW model has no
        provenance constraint);
      - one-hot statics: country, category, block (family/employment);
      - domain features: months_frozen (trailing run of Δy=0), advance streak
        (trailing run of Δy>0), recent retrogression (any Δy<0 in the last 6
        months), spread vs the OTHER table of the same cell (FAD-DFF gap),
        spread vs all_chargeability for the same category/table (NaN where not
        applicable — LightGBM/XGBoost handle NaN natively);
  * walk-forward: expanding, h=1, GLOBAL retrain every ``--retrain-months`` (12
    by default: a monthly global retrain would multiply campaign cost ~12x for
    a model whose cross-series pool barely changes month to month — documented
    cost/validity compromise, same spirit as NN_RETRAIN for the local nets);
  * training pool: every pilot series with >= 24 F observations donates rows
    (global learning), but ONLY the canonical evaluable cohort
    (``dataset.evaluable_series``) is scored;
  * evaluation: F-only mask via ``metrics.mase_by_series`` (AP2 scorer). For
    the hold-out frame its scale window equals the canonical pre-hold-out
    scale; for the selection frame the scorer uses the pre-first-forecast
    window (AP2 semantics) — comparable across models within this file.
  * hyperparameters: seeded from the LOCAL tuned winners
    (``reports/eval/tuned_params.json``, minus ``lags`` which encodes the local
    feature spec) with a high ``min_child_samples`` floor for the pooled table;
    fine HPO is epic AK/AQ work.

Outputs:
  * ``reports/eval/global_gbm_{table}.csv``            — per-series sel/hold MASE
  * ``reports/eval/global_gbm_forecasts_{table}.csv``  — level forecasts (for ensembles)

Usage (from repo root):
    ante/bin/python experiments/run_global_gbm.py --table both [--models lightgbm xgboost] [--limit 2]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# lightgbm/xgboost must load their OpenMP runtime BEFORE vp_model pulls in darts/torch:
# loading torch's bundled libomp first segfaults on macOS (same workaround as vp_model.models,
# which imports xgboost first for the same reason). Alphabetical order keeps isort happy.
import lightgbm  # noqa: F401
import numpy as np
import pandas as pd
import xgboost  # noqa: F401

from vp_model import config, dataset, metrics, preprocess

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "reports" / "eval"
TUNED = OUT_DIR / "tuned_params.json"
log = config.get_logger("global_gbm")

DY_LAGS = (1, 2, 3, 6, 12, 24)
RETRO_WINDOW = 6  # months scanned for a recent retrogression
MIN_DONOR_F = 24  # series with fewer real F obs than this donate no training rows
CALENDAR_COLS = ("month_sin", "month_cos", "fiscal_sin", "fiscal_cos")  # no 'year' (see module docstring)
STATIC_COLS = ("country", "category", "block")
MIN_CHILD_FLOOR = 50  # pooled table has ~15k rows; low leaf minima overfit single series

# Bridge defaults when tuned_params.json has no winning entry for the table.
DEFAULT_PARAMS: dict[str, dict] = {
    "lightgbm": {"learning_rate": 0.03, "n_estimators": 400, "num_leaves": 31, "min_child_samples": MIN_CHILD_FLOOR},
    "xgboost": {"learning_rate": 0.03, "n_estimators": 400, "max_depth": 6, "min_child_weight": MIN_CHILD_FLOOR},
}


def dense_level(raw: pd.Series) -> pd.Series:
    """Raw F series -> regular monthly level (mirror of models.to_timeseries fill)."""
    full = pd.date_range(raw.index.min(), raw.index.max(), freq="MS")
    return raw.reindex(full).astype("float64").interpolate(method="linear", limit_area="inside")


def _trailing_streak(flags: pd.Series) -> pd.Series:
    """Length of the run of True values ENDING at each position (inclusive)."""
    f = flags.astype(bool)
    groups = (~f).cumsum()
    return f.groupby(groups).cumsum().astype("float64")


def build_series_frame(
    level: pd.Series,
    *,
    other_table_level: pd.Series | None = None,
    allcharg_level: pd.Series | None = None,
) -> pd.DataFrame:
    """Feature/target frame for ONE series, indexed by target month t.

    Every feature uses information available at the origin (<= t-1): lag/streak
    features are computed on Δy and shifted by one month; the two spread
    features use the last known level of the reference series at t-1 (forward-
    filled — a bulletin value stands until superseded). Calendar features
    describe the target month itself (deterministic, hence causal).
    """
    dy = preprocess.difference(level)
    frame = pd.DataFrame(index=level.index)
    frame["target_dy"] = dy
    frame["prev_level"] = level.shift(1)
    for lag in DY_LAGS:
        frame[f"dy_lag{lag}"] = dy.shift(lag)
    frame["months_frozen"] = _trailing_streak(dy == 0).shift(1)
    frame["advance_streak"] = _trailing_streak(dy > 0).shift(1)
    frame["retro_recent"] = (dy < 0).rolling(RETRO_WINDOW, min_periods=1).max().shift(1)
    cal = preprocess.calendar_features(pd.DatetimeIndex(level.index))
    for col in CALENDAR_COLS:
        frame[col] = cal[col].to_numpy()
    for name, ref in (("spread_other_table", other_table_level), ("spread_vs_allcharg", allcharg_level)):
        if ref is None:
            frame[name] = np.nan
        else:
            ref_ff = ref.reindex(level.index.union(ref.index)).ffill().reindex(level.index)
            frame[name] = (level - ref_ff).shift(1)
    return frame


def _load_level(country: str, category: str, table: str) -> pd.Series | None:
    try:
        return dense_level(dataset.load_series(country, category, table).astype("float64"))
    except KeyError:
        return None


def build_panel_frame(table: str, limit: int | None) -> tuple[pd.DataFrame, set[tuple[str, str]]]:
    """Stacked feature table for ``table`` + the set of (country, category) to score.

    Donors: every pilot series with >= MIN_DONOR_F real F observations.
    Scored: the canonical evaluable cohort (``--limit`` caps it for smokes; the
    capped run still pools ALL donor rows so the global model is realistic).
    """
    eligible = dataset.evaluable_series()
    eligible = eligible[eligible["table"] == table]
    eval_keys = {(r.country, r.category) for r in eligible.itertuples()}
    if limit:
        eval_keys = set(sorted(eval_keys)[:limit])

    frames: list[pd.DataFrame] = []
    for block in ("family", "employment"):
        cat = dataset.list_series(table=table, block=block, min_trainable=MIN_DONOR_F)
        for r in cat.itertuples():
            raw = dataset.load_series(r.country, r.category, table).astype("float64")
            level = dense_level(raw)
            other = _load_level(r.country, r.category, "DFF" if table == "FAD" else "FAD")
            allch = _load_level("all_chargeability", r.category, table) if r.country != "all_chargeability" else None
            f = build_series_frame(level, other_table_level=other, allcharg_level=allch)
            f["country"], f["category"], f["block"] = r.country, r.category, block
            f["ds"] = f.index
            f["is_f_date"] = f.index.isin(raw.index)
            # evaluation origins: expanding window from MIN_TRAIN[table] (protocol)
            f["eval_row"] = False
            if (r.country, r.category) in eval_keys:
                f.iloc[config.MIN_TRAIN[table] :, f.columns.get_loc("eval_row")] = True
            f["holdout"] = f.index >= level.index[-config.HOLDOUT]
            frames.append(f.reset_index(drop=True))
    panel = pd.concat(frames, ignore_index=True)
    return panel[panel["target_dy"].notna()].reset_index(drop=True), eval_keys


def _make_model(name: str, table: str, seed: int):  # noqa: ANN202 — heavy libs typed at runtime only
    """GBM regressor seeded from the local tuned winners (AJ5 bridge; see docstring)."""
    params = dict(DEFAULT_PARAMS[name])
    if TUNED.exists():
        entry = json.loads(TUNED.read_text()).get(name, {}).get(f"{table}_family", {})
        if entry.get("improved"):
            tuned = {k: v for k, v in entry.get("best_params", {}).items() if k != "lags"}
            params.update(tuned)
    key = "min_child_samples" if name == "lightgbm" else "min_child_weight"
    params[key] = max(int(params.get(key, 0)), MIN_CHILD_FLOOR)
    if name == "lightgbm":
        from lightgbm import LGBMRegressor

        return LGBMRegressor(**params, random_state=seed, verbose=-1)
    from xgboost import XGBRegressor

    return XGBRegressor(**params, random_state=seed)


def _design(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """(X, y) with one-hot statics; column order is fixed by the full panel."""
    feature_cols = [c for c in panel.columns if c.startswith(("dy_lag", "months_", "advance_", "retro_", "spread_"))]
    feature_cols += list(CALENDAR_COLS)
    x = pd.concat(
        [panel[feature_cols], pd.get_dummies(panel[list(STATIC_COLS)], columns=list(STATIC_COLS))], axis=1
    ).astype("float64")
    return x, panel["target_dy"].astype("float64")


def walk_forward(panel: pd.DataFrame, name: str, table: str, retrain_months: int, seed: int) -> pd.DataFrame:
    """Expanding h=1 walk-forward with a global retrain every ``retrain_months``.

    Each block [t0, t0+retrain) is predicted by a model trained ONLY on rows
    whose target month precedes t0 (leakage-free: features of those rows use
    information <= their own t-1 < t0). Δy predictions are reintegrated onto
    the last known level (causal, same contract as ``models.Differenced``).
    """
    x_all, y_all = _design(panel)
    eval_dates = np.sort(panel.loc[panel["eval_row"], "ds"].unique())
    preds = pd.Series(np.nan, index=panel.index)
    block_starts = pd.DatetimeIndex(eval_dates)[::retrain_months]
    for i, t0 in enumerate(block_starts):
        t1 = block_starts[i + 1] if i + 1 < len(block_starts) else pd.Timestamp.max
        train_mask = (panel["ds"] < t0).to_numpy()
        pred_mask = (panel["eval_row"] & (panel["ds"] >= t0) & (panel["ds"] < t1)).to_numpy()
        if not pred_mask.any():
            continue
        model = _make_model(name, table, seed)
        model.fit(x_all[train_mask], y_all[train_mask])
        preds.iloc[np.flatnonzero(pred_mask)] = model.predict(x_all[pred_mask])
        log.info(
            "%s %s block %s: train=%d predict=%d", name, table, t0.strftime("%Y-%m"), train_mask.sum(), pred_mask.sum()
        )
    out = panel.loc[preds.notna(), ["country", "category", "block", "ds", "prev_level", "holdout", "is_f_date"]].copy()
    out["forecast"] = (out["prev_level"] + preds[preds.notna()]).to_numpy()  # causal reintegration
    out["model"] = f"global_{name}"
    return out.rename(columns={"ds": "date"})


def score_and_write(fc: pd.DataFrame, table: str, out_name: str) -> pd.DataFrame:
    """Per-series sel/hold MASE via the canonical F-only scorer (AP2/B1)."""
    rows = []
    for model, g in fc.groupby("model"):
        sel = metrics.mase_by_series(g[~g["holdout"]], table)
        hold = metrics.mase_by_series(g[g["holdout"]], table)
        for (country, category), hold_mase in hold.items():
            rows.append(
                {
                    "model": model,
                    "country": country,
                    "category": category,
                    "table": table,
                    "sel_mase": round(float(sel.get((country, category), float("nan"))), 4),
                    "hold_mase": round(float(hold_mase), 4),
                }
            )
    df = pd.DataFrame(rows)
    out = OUT_DIR / f"{out_name}_{table}.csv"
    df.to_csv(out, index=False)
    fc_out = OUT_DIR / f"{out_name}_forecasts_{table}.csv"
    fc[["model", "country", "category", "block", "date", "forecast", "holdout", "is_f_date"]].to_csv(
        fc_out, index=False
    )
    if not df.empty:
        print(df.groupby("model")[["sel_mase", "hold_mase"]].mean().round(4))
    print(f"written {out.relative_to(ROOT)} ({len(df)} series rows) + {fc_out.name}")
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="Global GBM on the stacked panel (AL2)")
    ap.add_argument("--table", default="both", choices=["FAD", "DFF", "both"])
    ap.add_argument("--models", nargs="+", default=["lightgbm"], choices=["lightgbm", "xgboost"])
    ap.add_argument("--retrain-months", type=int, default=12)
    ap.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    ap.add_argument("--limit", type=int, default=None, help="cap the number of SCORED series (smoke)")
    args = ap.parse_args()
    config.seed_everything(args.seed)
    for table in ("FAD", "DFF") if args.table == "both" else (args.table,):
        panel, eval_keys = build_panel_frame(table, args.limit)
        log.info("[%s] pooled rows=%d, scored series=%d", table, len(panel), len(eval_keys))
        fc = pd.concat(
            [walk_forward(panel, name, table, args.retrain_months, args.seed) for name in args.models],
            ignore_index=True,
        )
        score_and_write(fc, table, "global_gbm")


if __name__ == "__main__":
    main()
