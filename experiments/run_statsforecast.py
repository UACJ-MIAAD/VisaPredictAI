"""AL1 — statsforecast Auto* family search under the local walk-forward protocol.

The current champion (darts ``ets``/``theta``) is a SINGLE spec (plus the small
AJ4 in-family search). This script searches the whole family with Nixtla's
statsforecast: **AutoETS, AutoTheta, AutoCES, DynamicOptimizedTheta**
(AutoCES/DOT are strong M4-monthly performers absent from the local pool).

Runs in the ISOLATED venv ``ante_nf`` (statsforecast does not build on the main
py3.14 env because of its ``scipy<1.16`` pin; in ante_nf it is installed with
``pip install --no-deps statsforecast`` + cloudpickle/statsmodels/tqdm/
threadpoolctl/fugue on top of the existing scipy 1.17 — verified working).
Because ``ante_nf`` has no darts/vp_model, this script follows the CSV-bridge
pattern of ``run_global_deep.py``: panel parquet in, tidy CSV out, and the
F-only mask + naive scale are REPLICATED locally (``vp_model.metrics`` imports
darts, so it cannot be imported here; ``tests/test_newmodels_brutal.py`` anchors
the replicas against the canonical implementations, same contract as the
constants pinned below).

Protocol equivalence with ``vp_model.walkforward``:
  * series = raw F observations, densified to a regular monthly grid with linear
    interpolation of internal gaps (mirror of ``models.to_timeseries``);
  * expanding walk-forward, h=1, step=1, from MIN_TRAIN[table] onward, via the
    native ``StatsForecast.cross_validation`` per series (per-series window
    counts differ, so one call per series);
  * ``refit`` (default 1 = re-select and re-fit at EVERY origin, the honest
    expensive mode; ``--refit 12`` re-selects annually and rolls the fitted
    model forward in between — statsforecast updates states without re-fitting);
  * metrics: MASE with the seasonal-naive (m=12) scale computed on the raw
    F-only series BEFORE the hold-out split, scored ONLY on real F dates (B1).

Output: ``reports/eval/statsforecast_{table}.csv`` with columns compatible with
``model_comparison_*`` (model, country, category, table, sel_mase, hold_mase, ...).

AL7 — documented decision NOT to add Moirai / Lag-Llama / MSTL / standard
hierarchical reconciliation: Moirai and Lag-Llama are zero-shot foundation
models in the same class as Chronos, which already scored ~0.225 MASE on this
panel — a second and third foundation row adds table rows, not insight (the
realistic foundation path is fine-tuning, tracked separately as AL6). MSTL
targets multiple-seasonality decomposition; the EDA census found these series
NON-seasonal (0/74 stationary, F_S ~ 0), so there is no seasonal structure to
decompose. Standard hierarchical reconciliation (MinT et al.) requires series
that AGGREGATE (children sum to parents); visa cutoff dates are order
statistics, not additive quantities — the panel has no summing hierarchy, only
order constraints, which are exploited instead by ``apply_cone_constraints.py``.

Usage (from repo root):
    ante_nf/bin/python experiments/run_statsforecast.py --table both [--refit 1] [--limit 2]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "processed" / "visa_panel_long.parquet"
OUT_DIR = ROOT / "reports" / "eval"
PILOT = ("mexico", "india", "china", "philippines", "all_chargeability")
# Constants re-declared because this script runs in ante_nf (no vp_model).
# MUST match vp_model.config — anchored by tests/test_newmodels_brutal.py.
HOLDOUT = 24  # = vp_model.config.HOLDOUT
MIN_TRAIN = {"FAD": 60, "DFF": 36}  # = vp_model.config.MIN_TRAIN
MIN_BACKTEST_BUFFER = 6  # = vp_model.config.MIN_BACKTEST_BUFFER
SEASONAL_M = 12  # = vp_model.config.SEASONAL_PERIOD
SEASON_LENGTH = 12  # season_length passed to the statsforecast models


def seasonal_naive_mae(values: np.ndarray, m: int = SEASONAL_M) -> float:
    """Replica of ``vp_model.metrics.seasonal_naive_mae`` (anchored by test).

    Degenerate scale (constant / too-short series) -> NaN, never a silent 1.0.
    """
    v = np.asarray(values, dtype="float64")
    diffs = np.abs(v[m:] - v[:-m]) if len(v) > m else np.abs(np.diff(v))
    s = float(np.mean(diffs)) if len(diffs) else 0.0
    return s if np.isfinite(s) and s > 0 else float("nan")


def naive_scale_before(full: pd.Series, cutoff: pd.Timestamp, m: int = SEASONAL_M) -> float:
    """Replica of ``vp_model.metrics.naive_scale_before`` (date-aligned, leakage-free)."""
    train = full[full.index < cutoff].astype("float64").to_numpy()
    return seasonal_naive_mae(train, m)


def densify(raw: pd.Series) -> pd.Series:
    """Raw F series -> regular monthly grid, internal gaps linearly interpolated.

    Mirror of the local path (``preprocess.to_regular_monthly`` followed by
    darts ``fill_missing_values(fill="auto")``): ALL internal gaps end up
    linearly interpolated for training continuity; the filled months are never
    scored (the B1 mask keeps evaluation on real F dates only).
    """
    full = pd.date_range(raw.index.min(), raw.index.max(), freq="MS")
    return raw.reindex(full).astype("float64").interpolate(method="linear", limit_area="inside")


def load_f_series(table: str) -> dict[tuple[str, str, str], pd.Series]:
    """Eligible raw F-only series keyed by (country, block, category).

    Eligibility mirrors ``vp_model.dataset.is_evaluable``: >= MIN_TRAIN['FAD'] +
    HOLDOUT real F observations (the published 84-F criterion) AND a densified
    span long enough for the walk-forward.
    """
    df = pd.read_parquet(PANEL)
    df = df[(df["table"] == table) & (df["status"] == "F") & (df["country"].isin(PILOT))]
    out: dict[tuple[str, str, str], pd.Series] = {}
    for (country, block, category), g in df.groupby(["country", "block", "category"], sort=True):
        g = g.sort_values("bulletin_date")
        s = pd.Series(
            g["days_since_base"].astype("float64").to_numpy(),
            index=pd.DatetimeIndex(pd.to_datetime(g["bulletin_date"])),
        )
        n_dense = (s.index.max().to_period("M") - s.index.min().to_period("M")).n + 1
        if len(s) >= MIN_TRAIN["FAD"] + HOLDOUT and n_dense >= MIN_TRAIN[table] + HOLDOUT + MIN_BACKTEST_BUFFER:
            out[(country, block, category)] = s
    return out


def _build_models():  # noqa: ANN202 — statsforecast types only exist in ante_nf
    """The 4 Auto* searchers, aliased to the pool's lowercase naming convention."""
    from statsforecast.models import AutoCES, AutoETS, AutoTheta, DynamicOptimizedTheta

    return [
        AutoETS(season_length=SEASON_LENGTH, alias="sf_autoets"),
        AutoTheta(season_length=SEASON_LENGTH, alias="sf_autotheta"),
        AutoCES(season_length=SEASON_LENGTH, alias="sf_autoces"),
        DynamicOptimizedTheta(season_length=SEASON_LENGTH, alias="sf_dotheta"),
    ]


def backtest_series(dense: pd.Series, table: str, refit: int) -> pd.DataFrame | None:
    """Walk-forward CV of the 4 models on ONE densified series (h=1, step=1).

    Returns the statsforecast cv frame (ds, cutoff, y, <model columns>) or None
    when the whole series fails (isolated, like run_global_deep per-model guard).
    """
    from statsforecast import StatsForecast

    n_windows = len(dense) - MIN_TRAIN[table]
    df = pd.DataFrame({"unique_id": "s", "ds": dense.index, "y": dense.to_numpy()})
    sf = StatsForecast(models=_build_models(), freq="MS", n_jobs=1)
    return sf.cross_validation(df=df, h=1, step_size=1, n_windows=n_windows, refit=refit)


def score(cv: pd.DataFrame, raw: pd.Series, dense: pd.Series, model_cols: list[str]) -> list[dict]:
    """Per-model sel/hold MASE with the F-only mask and the shared naive scale (B1)."""
    split = dense.index[-HOLDOUT]
    scale = naive_scale_before(raw, split)
    cv = cv[cv["ds"].isin(raw.index)]  # B1: score only real F observations
    actual = raw.reindex(pd.DatetimeIndex(cv["ds"])).to_numpy()
    rows = []
    for col in model_cols:
        err = np.abs(actual - cv[col].to_numpy(dtype="float64"))
        sel = err[(cv["ds"] < split).to_numpy()]
        hold = err[(cv["ds"] >= split).to_numpy()]
        rows.append(
            {
                "model": col,
                "sel_mase": float(np.mean(sel)) / scale if len(sel) else float("nan"),
                "sel_mae": float(np.mean(sel)) if len(sel) else float("nan"),
                "sel_n": int(len(sel)),
                "hold_mase": float(np.mean(hold)) / scale if len(hold) else float("nan"),
                "hold_mae": float(np.mean(hold)) if len(hold) else float("nan"),
                "hold_n": int(len(hold)),
            }
        )
    return rows


def run_table(table: str, refit: int, limit: int | None) -> Path:
    series = load_f_series(table)
    keys = list(series)[:limit] if limit else list(series)
    print(f"[{table}] {len(keys)} series (refit={refit})")
    all_rows: list[dict] = []
    model_cols = [m.alias for m in _build_models()]
    for i, key in enumerate(keys, 1):
        country, block, category = key
        raw = series[key]
        dense = densify(raw)
        t0 = time.time()
        try:
            cv = backtest_series(dense, table, refit)
        except Exception as e:  # noqa: BLE001 — one failing series must not abort the campaign
            print(f"  ✗ {country}/{category}: {type(e).__name__}: {str(e)[:120]}")
            continue
        for row in score(cv, raw, dense, model_cols):
            all_rows.append({"country": country, "block": block, "category": category, "table": table, **row})
        print(f"  ✓ {i}/{len(keys)} {country}/{category} ({time.time() - t0:.1f}s)")
    df = pd.DataFrame(all_rows)
    out = OUT_DIR / f"statsforecast_{table}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "model",
        "country",
        "block",
        "category",
        "table",
        "sel_mase",
        "sel_mae",
        "sel_n",
        "hold_mase",
        "hold_mae",
        "hold_n",
    ]
    df[cols].to_csv(out, index=False)
    if not df.empty:
        print(df.groupby("model")[["sel_mase", "hold_mase"]].mean().round(4))
    print(f"written {out.relative_to(ROOT)} ({len(df)} rows)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--table", default="both", choices=["FAD", "DFF", "both"])
    ap.add_argument("--refit", type=int, default=1, help="re-select/re-fit every N origins (1 = every origin)")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of series (smoke)")
    args = ap.parse_args()
    for table in ("FAD", "DFF") if args.table == "both" else (args.table,):
        run_table(table, args.refit, args.limit)


if __name__ == "__main__":
    main()
