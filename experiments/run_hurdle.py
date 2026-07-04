"""AL3 — global hurdle model: P(move) x conditional magnitude.

Δy on this panel is INTERMITTENT (~45% of the monthly deltas are exactly zero:
the cutoff simply does not move). A single regressor is forced to average the
zeros into every prediction; the hurdle decomposition models the structure of
the problem directly with two global LightGBMs sharing the AL2 feature set
(``run_global_gbm.build_panel_frame``):

  * a binary classifier for "does the cutoff move this month?" (Δy != 0);
  * a regressor for the SIGNED Δy conditional on movement. Signed, not |Δy|:
    retrogressions are legitimate within-F magnitudes (2.4% of the panel) and
    an absolute-value target would force the model to always predict forward
    movement.

Point forecast = P(move) * E[Δy | move] — the EXPECTED VALUE of the hurdle
distribution. Documented alternative (``--threshold``): predict 0 when
P(move) < 0.5 and E[Δy | move] otherwise; it optimizes the 0/1 decision rather
than the mean and is usually worse in MAE terms, but it is reported for the
campaign to verify.

Scope defensibility: both stages operate strictly INSIDE regime F (a zero Δy
is a published F date identical to the previous month). This does NOT touch
the C/F/U regime classification excluded from the project scope by the
director — it classifies magnitude within F, not regime.

Protocol and outputs mirror ``run_global_gbm.py`` (expanding walk-forward,
h=1, global retrain every 12 months, causal reintegration, F-only scoring via
``metrics.mase_by_series``) -> ``reports/eval/hurdle_{table}.csv``.

Usage (from repo root):
    ante/bin/python experiments/run_hurdle.py --table both [--threshold] [--limit 2]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# lightgbm must load its OpenMP runtime BEFORE vp_model pulls in darts/torch
# (macOS double-libomp segfault — same workaround as run_global_gbm/vp_model.models).
import lightgbm  # noqa: F401
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # scripts run as `ante/bin/python experiments/<name>.py` from the root

from experiments.run_global_gbm import _design, build_panel_frame, score_and_write  # noqa: E402
from vp_model import config  # noqa: E402

log = config.get_logger("hurdle")

MIN_CHILD = 50  # pooled table; same floor as run_global_gbm
CLS_PARAMS: dict = {"learning_rate": 0.05, "n_estimators": 300, "num_leaves": 31, "min_child_samples": MIN_CHILD}
REG_PARAMS: dict = {"learning_rate": 0.03, "n_estimators": 400, "num_leaves": 31, "min_child_samples": MIN_CHILD}


def hurdle_point(p_move: np.ndarray, e_move: np.ndarray, threshold: bool) -> np.ndarray:
    """Combine the two stages: expected value (default) or 0/1 threshold rule."""
    if threshold:
        return np.where(p_move >= 0.5, e_move, 0.0)
    return p_move * e_move


def walk_forward_hurdle(
    panel: pd.DataFrame, table: str, retrain_months: int, seed: int, threshold: bool
) -> pd.DataFrame:
    """Expanding h=1 walk-forward of the two-stage hurdle (global retrain per block).

    Same leakage discipline as ``run_global_gbm.walk_forward``: each block is
    predicted by models trained only on rows whose target month precedes it.
    The regressor trains on MOVING rows only (Δy != 0) — that is the hurdle.
    """
    from lightgbm import LGBMClassifier, LGBMRegressor

    x_all, y_all = _design(panel)
    moved_all = (y_all != 0).to_numpy()
    eval_dates = np.sort(panel.loc[panel["eval_row"], "ds"].unique())
    preds = pd.Series(np.nan, index=panel.index)
    block_starts = pd.DatetimeIndex(eval_dates)[::retrain_months]
    for i, t0 in enumerate(block_starts):
        t1 = block_starts[i + 1] if i + 1 < len(block_starts) else pd.Timestamp.max
        train = (panel["ds"] < t0).to_numpy()
        pred = (panel["eval_row"] & (panel["ds"] >= t0) & (panel["ds"] < t1)).to_numpy()
        if not pred.any():
            continue
        cls = LGBMClassifier(**CLS_PARAMS, random_state=seed, verbose=-1)
        cls.fit(x_all[train], moved_all[train])
        reg = LGBMRegressor(**REG_PARAMS, random_state=seed, verbose=-1)
        reg.fit(x_all[train & moved_all], y_all[train & moved_all])
        p_move = cls.predict_proba(x_all[pred])[:, 1]
        e_move = reg.predict(x_all[pred])
        preds.iloc[np.flatnonzero(pred)] = hurdle_point(p_move, e_move, threshold)
        log.info(
            "%s block %s: train=%d (moving=%d) predict=%d",
            table,
            t0.strftime("%Y-%m"),
            train.sum(),
            (train & moved_all).sum(),
            pred.sum(),
        )
    out = panel.loc[preds.notna(), ["country", "category", "block", "ds", "prev_level", "holdout", "is_f_date"]].copy()
    out["forecast"] = (out["prev_level"] + preds[preds.notna()]).to_numpy()  # causal reintegration
    out["model"] = "hurdle_lgbm_thr" if threshold else "hurdle_lgbm"
    return out.rename(columns={"ds": "date"})


def main() -> None:
    ap = argparse.ArgumentParser(description="Global hurdle model P(move) x magnitude (AL3)")
    ap.add_argument("--table", default="both", choices=["FAD", "DFF", "both"])
    ap.add_argument("--retrain-months", type=int, default=12)
    ap.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    ap.add_argument("--threshold", action="store_true", help="0/1 rule instead of the expected value")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of SCORED series (smoke)")
    args = ap.parse_args()
    config.seed_everything(args.seed)
    for table in ("FAD", "DFF") if args.table == "both" else (args.table,):
        panel, eval_keys = build_panel_frame(table, args.limit)
        share_zero = float((panel["target_dy"] == 0).mean())
        log.info(
            "[%s] pooled rows=%d, scored series=%d, share of zero deltas=%.2f",
            table,
            len(panel),
            len(eval_keys),
            share_zero,
        )
        fc = walk_forward_hurdle(panel, table, args.retrain_months, args.seed, args.threshold)
        score_and_write(fc, table, "hurdle_thr" if args.threshold else "hurdle")  # variants keep separate CSVs


if __name__ == "__main__":
    main()
