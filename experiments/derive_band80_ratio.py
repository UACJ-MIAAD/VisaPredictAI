"""Calibrate prediction-band scales from the prospective ledger, on a DISJOINT vintage split.

Two artifacts come out of the same standardized-error table (|error| / 1-step conformal
half-width, per ledger row):

1. **Legacy scalar** ``BAND80_RATIO`` (``derive()``): P80 of the standardized error over the
   calibration vintages, pooled across horizons. Kept for provenance/fallback; if the printed
   value differs from ``config.BAND80_RATIO``, update config.py.
2. **Per-horizon scales** ``q_{table, level, h}`` (``derive_by_h()``, AN2): empirical quantiles
   of the standardized error per table x level {80, 95} x horizon h=1..12, written to
   ``reports/prospective/pi_scale_by_h.json``. These replace the ``sqrt(h)`` growth heuristic
   in ``generate_web_forecasts``: the deployed half-width at horizon h becomes
   ``half95_1step * q_{table, level, h}``. The prospective scorecard showed why: with sqrt(h)
   the 80% band decays from cov 0.93 (h=1) to 0.72 (h=12).

Split discipline (unchanged from the scalar version): quantiles are fitted ONLY on the
``config.BAND80_CAL_VINTAGES`` vintages; coverage is validated on the REMAINING vintages
(held-out), so the reported coverage is out-of-sample, not circular. Standardization inverts
the sqrt(h) growth the ledger rows were frozen with: half95_1step = (hi95 - lo95) / 2 / sqrt(h).

Hygiene (AN7): quantiles use ``method="higher"`` (conservative order statistic); cells with
fewer than ``MIN_CELL_N`` calibration pairs are omitted (the consumer falls back to sqrt(h));
every validated coverage carries a Jeffreys CI and its n, with an ``insufficient_n`` flag
below N_FLOOR.

Usage:  ante/bin/python experiments/derive_band80_ratio.py
Writes reports/prospective/pi_scale_by_h.json (the scalar path stays read-only).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import config, dataset
from vp_model.intervals import jeffreys_ci

REPORTS = Path(__file__).resolve().parent.parent / "reports"
SCALES_PATH = REPORTS / "prospective" / "pi_scale_by_h.json"
LEVELS = (80, 95)  # nominal band levels served by the web demo
MIN_CELL_N = 30  # minimum calibration pairs for a (table, h) quantile to be emitted
N_FLOOR = 30  # below this, a validated coverage is flagged insufficient_n (AN7)


def _std_errors() -> pd.DataFrame:
    """One row per realized ledger forecast: (origin, table, h, std).

    ``std`` = |error| / half95_1step, where half95_1step un-does the sqrt(h) growth the
    frozen rows were published with. Standardizing by the series' own 1-step conformal
    half-width makes errors comparable across series, so quantiles pool cleanly.
    """
    log = pd.read_csv(REPORTS / "prospective" / "forecast_log.csv")
    # Era guard (audit): dividing by sqrt(h) only recovers the 1-step half-width for
    # vintages frozen with the sqrt-h heuristic. Once q_h vintages enter the ledger
    # (band_method column, added 4-jul-2026), mixing eras would corrupt the very
    # quantiles that feed the deployed bands. Legacy rows (no column/NaN) are sqrt-h.
    if "band_method" in log.columns:
        log = log[log["band_method"].isna() | log["band_method"].astype(str).str.startswith("sqrt")]
    actuals = dataset.actuals_F()
    rows = []
    for r in log.itertuples():
        a = actuals.get((r.country, r.category, r.table, r.date))
        if a is None:
            continue
        half95_1step = (r.hi95 - r.lo95) / 2.0 / math.sqrt(r.h)
        if half95_1step <= 0:
            continue
        rows.append((r.origin, r.table, int(r.h), abs(r.days - a) / half95_1step))
    return pd.DataFrame(rows, columns=["origin", "table", "h", "std"])


def compute_scales(cal: pd.DataFrame, min_cell_n: int = MIN_CELL_N) -> tuple[dict, dict]:
    """(scales, n_cal) from calibration standardized errors.

    scales[table][str(level)][str(h)] = conservative empirical quantile of ``std`` at
    level/100 for that (table, h) cell, made monotone non-decreasing in h (running max:
    forecast error does not shrink with horizon; the envelope irons out sampling noise).
    Cells with n < min_cell_n are omitted entirely (consumer falls back to sqrt(h)).
    """
    scales: dict[str, dict[str, dict[str, float]]] = {}
    n_cal: dict[str, dict[str, int]] = {}
    for table, gt in cal.groupby("table"):
        per_level: dict[str, dict[str, float]] = {str(lv): {} for lv in LEVELS}
        counts: dict[str, int] = {}
        prev = {lv: 0.0 for lv in LEVELS}
        for h in sorted(gt["h"].unique()):
            cell = gt.loc[gt["h"] == h, "std"].to_numpy()
            counts[str(int(h))] = int(len(cell))
            if len(cell) < min_cell_n:
                continue
            for lv in LEVELS:
                # The 95 tail is an extreme order statistic: with n<60 a single
                # outlier at some h pins the whole monotone envelope (audit: DFF
                # q95 sat flat at 5.88 from the h=1 cell). Omit the cell instead
                # (consumer falls back to sqrt(h) there).
                if lv >= 95 and len(cell) < 2 * min_cell_n:
                    continue
                q = float(np.quantile(cell, lv / 100.0, method="higher"))
                if int(h) == 1:
                    # never deploy an h=1 band NARROWER than the calibrated
                    # 1-step conformal width (audit: FAD q95(1)=0.44).
                    q = max(q, 1.0)
                q = max(q, prev[lv])  # monotone envelope in h
                prev[lv] = q
                per_level[str(lv)][str(int(h))] = round(q, 4)
        scales[str(table)] = per_level
        n_cal[str(table)] = counts
    return scales, n_cal


def validate_scales(evl: pd.DataFrame, scales: dict) -> dict:
    """Held-out coverage of the q_h bands per table x level, with Jeffreys CI and n (AN7)."""
    out: dict[str, dict[str, dict]] = {}
    for table, gt in evl.groupby("table"):
        out[str(table)] = {}
        for lv in LEVELS:
            qmap = scales.get(str(table), {}).get(str(lv), {})
            g = gt[gt["h"].astype(str).isin(qmap)]
            if g.empty:
                out[str(table)][str(lv)] = {"n": 0, "insufficient_n": True}
                continue
            hit = g["std"].to_numpy() <= g["h"].astype(str).map(qmap).to_numpy()
            k, n = int(hit.sum()), int(len(hit))
            lo, hi = jeffreys_ci(k, n)
            out[str(table)][str(lv)] = {
                "coverage": round(k / n, 3),
                "ci95": [round(lo, 3), round(hi, 3)],
                "n": n,
                "insufficient_n": n < N_FLOOR,
            }
    return out


def derive_by_h() -> dict:
    """Full per-horizon payload: calibrate on BAND80_CAL_VINTAGES, validate on the rest."""
    df = _std_errors()
    cal_set = set(config.BAND80_CAL_VINTAGES)
    cal = df[df["origin"].isin(cal_set)]
    evl = df[~df["origin"].isin(cal_set)]
    if cal.empty or evl.empty:
        raise ValueError(f"empty split: cal={len(cal)} eval={len(evl)} (BAND80_CAL_VINTAGES present?)")
    scales, n_cal = compute_scales(cal)
    return {
        "what": "per-horizon prediction-band scales: half_{level,h} = half95_1step * q_{table,level,h}",
        "method": (
            "empirical quantile (method='higher') of |error|/half95_1step per table x horizon, "
            "monotone in h; calibrated on cal_vintages, validated on the remaining vintages"
        ),
        "cal_vintages": sorted(cal_set),
        "levels": list(LEVELS),
        "min_cell_n": MIN_CELL_N,
        "n_cal_rows": int(len(cal)),
        "n_eval_rows": int(len(evl)),
        "scales": scales,
        "n_cal": n_cal,
        "validation_heldout": validate_scales(evl, scales),
    }


def derive() -> dict:
    """Legacy scalar BAND80_RATIO (pooled across horizons) — kept for provenance/fallback.

    Unlike ``_std_errors`` this standardizes by the AS-PUBLISHED half95 (sqrt(h) growth
    included), matching how the scalar has always been defined against config.BAND80_RATIO.
    """
    log = pd.read_csv(REPORTS / "prospective" / "forecast_log.csv")
    actuals = dataset.actuals_F()
    rows = []
    for r in log.itertuples():
        a = actuals.get((r.country, r.category, r.table, r.date))
        if a is None:
            continue
        half95 = (r.hi95 - r.lo95) / 2.0
        if half95 <= 0:
            continue
        rows.append((r.origin, abs(r.days - a) / half95))
    df = pd.DataFrame(rows, columns=["origin", "std"])
    cal_set = set(config.BAND80_CAL_VINTAGES)
    cal = df[df["origin"].isin(cal_set)]
    evl = df[~df["origin"].isin(cal_set)]
    if cal.empty or evl.empty:
        raise ValueError(f"empty split: cal={len(cal)} eval={len(evl)} (BAND80_CAL_VINTAGES present?)")

    ratio = float(np.quantile(cal["std"], 0.80))
    k, n = int((evl["std"] <= ratio).sum()), int(len(evl))
    lo, hi = jeffreys_ci(k, n)
    return {
        "ratio": round(ratio, 4),
        "n_cal": int(len(cal)),
        "n_eval": n,
        "cov80_cal_insample": round(float((cal["std"] <= ratio).mean()), 3),
        "cov80_eval_heldout": round(k / n, 3),
        "cov80_eval_ci95": [round(lo, 3), round(hi, 3)],
        "cal_vintages": sorted(cal_set),
    }


if __name__ == "__main__":
    r = derive()
    print(f"BAND80_RATIO calibrated (P80 on {r['cal_vintages']}, n={r['n_cal']}) = {r['ratio']}")
    print(f"cov80 in-sample (calibration)    = {r['cov80_cal_insample']}  (~0.80 by construction)")
    print(
        f"cov80 HELD-OUT (n={r['n_eval']})          = {r['cov80_eval_heldout']} "
        f"CI95 {r['cov80_eval_ci95']}  <-- honest number"
    )
    print(
        f"config.BAND80_RATIO current = {config.BAND80_RATIO}",
        "(matches)" if abs(r["ratio"] - config.BAND80_RATIO) < 1e-9 else "<- UPDATE in config.py",
    )

    payload = derive_by_h()
    SCALES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"\nper-horizon scales -> {SCALES_PATH} (cal rows={payload['n_cal_rows']})")
    for table, levels in payload["validation_heldout"].items():
        for lv, v in levels.items():
            if v.get("n"):
                print(
                    f"  {table} q_h band {lv}%: held-out coverage {v['coverage']} "
                    f"CI95 {v['ci95']} (n={v['n']}{', INSUFFICIENT n' if v['insufficient_n'] else ''})"
                )
