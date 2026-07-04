"""AL5 — cross-sectional coherence: project forecasts onto the feasible cone.

The Visa Bulletin publishes order constraints that individual per-series
forecasts ignore (they are free money the pool never exploited):

  * FAD <= DFF for the same (country, category, month): a filing date can never
    precede the final-action date in priority-date space (days_since_base).
    The panel has 6 REAL historical inversions, so the raw data occasionally
    violates it — but a coherent FORECAST should not.
  * oversubscribed country <= all_chargeability for the same (category, table,
    month): a per-country limit can only push the cutoff further back.

Projection (simple isotonic min/max, per the plan):
  * country cap: country' = min(country, all_chargeability) — the reference
    row (all_chargeability) is kept fixed and violating countries are clipped;
  * FAD/DFF pair: FAD' = min(FAD, DFF), DFF' = max(FAD, DFF) — the order
    statistics of the pair (the L2-optimal alternative, averaging the pair,
    changes both members; min/max preserves the two published values).
  Bands (lo80/hi80/lo95/hi95) are SHIFTED by the same delta as the point so
  their width (the calibrated uncertainty) is preserved.
  Passes run country-cap first, then FAD/DFF; the second pass can in principle
  re-open a country violation (DFF' moves up), so residual violations are
  COUNTED honestly after both passes.

This is a TOOL + METRIC, deliberately NOT wired into the web publisher
(``generate_web_forecasts.py``): whether the projection ships is an AQ-campaign
decision, taken with the violation counts produced here.

Outputs:
  * ``reports/eval/cone_violations.csv``      — violations pre/post per constraint x table
  * ``reports/eval/web_forecasts_cone.csv``   — the projected forecasts (tool artifact)

Usage (from repo root):
    ante/bin/python experiments/apply_cone_constraints.py [--input reports/prospective/web_forecasts.csv]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "reports" / "prospective" / "web_forecasts.csv"
OUT_DIR = ROOT / "reports" / "eval"
REFERENCE_COUNTRY = "all_chargeability"
OVERSUBSCRIBED = ("mexico", "india", "china", "philippines")
BAND_COLS = ("lo80", "hi80", "lo95", "hi95")


def count_fad_dff_violations(df: pd.DataFrame) -> int:
    """Cells (country, category, date) where the FAD forecast exceeds the DFF one."""
    wide = df.pivot_table(index=["country", "category", "date"], columns="table", values="days", aggfunc="first")
    if "FAD" not in wide.columns or "DFF" not in wide.columns:
        return 0
    both = wide.dropna(subset=["FAD", "DFF"])
    return int((both["FAD"] > both["DFF"]).sum())


def count_country_violations(df: pd.DataFrame) -> int:
    """Rows where an oversubscribed country forecast exceeds all_chargeability's."""
    ref = df[df["country"] == REFERENCE_COUNTRY].set_index(["category", "table", "date"])["days"]
    sub = df[df["country"].isin(OVERSUBSCRIBED)]
    ref_days = ref.reindex(pd.MultiIndex.from_frame(sub[["category", "table", "date"]])).to_numpy()
    mask = pd.notna(ref_days) & (sub["days"].to_numpy() > ref_days)
    return int(mask.sum())


def _shift_row(df: pd.DataFrame, idx, new_days: pd.Series) -> None:
    """Set ``days`` to ``new_days`` at ``idx`` and shift the bands by the same delta."""
    delta = new_days - df.loc[idx, "days"]
    for col in BAND_COLS:
        df.loc[idx, col] = df.loc[idx, col] + delta
    df.loc[idx, "days"] = new_days


def apply_country_cap(df: pd.DataFrame) -> pd.DataFrame:
    """country' = min(country, all_chargeability) per (category, table, date)."""
    df = df.copy()
    ref = df[df["country"] == REFERENCE_COUNTRY].set_index(["category", "table", "date"])["days"]
    sub_idx = df.index[df["country"].isin(OVERSUBSCRIBED)]
    keys = pd.MultiIndex.from_frame(df.loc[sub_idx, ["category", "table", "date"]])
    ref_days = pd.Series(ref.reindex(keys).to_numpy(), index=sub_idx)
    viol = ref_days.notna() & (df.loc[sub_idx, "days"] > ref_days)
    _shift_row(df, sub_idx[viol], ref_days[viol])
    return df


def apply_fad_dff(df: pd.DataFrame) -> pd.DataFrame:
    """(FAD', DFF') = (min, max) of the pair per (country, category, date)."""
    df = df.copy()
    key_cols = ["country", "category", "date"]
    fad = df[df["table"] == "FAD"].set_index(key_cols)
    dff = df[df["table"] == "DFF"].set_index(key_cols)
    common = fad.index.intersection(dff.index)
    bad = common[(fad.loc[common, "days"] > dff.loc[common, "days"]).to_numpy()]
    if len(bad):
        fad_pos = df.index[df["table"] == "FAD"][fad.index.get_indexer(bad)]
        dff_pos = df.index[df["table"] == "DFF"][dff.index.get_indexer(bad)]
        lo = pd.Series(dff.loc[bad, "days"].to_numpy(), index=fad_pos)  # min of the pair
        hi = pd.Series(fad.loc[bad, "days"].to_numpy(), index=dff_pos)  # max of the pair
        _shift_row(df, fad_pos, lo)
        _shift_row(df, dff_pos, hi)
    return df


def run(input_path: Path) -> pd.DataFrame:
    df = pd.read_csv(input_path)
    pre_country = count_country_violations(df)
    pre_pair = count_fad_dff_violations(df)
    projected = apply_fad_dff(apply_country_cap(df))
    post_country = count_country_violations(projected)
    post_pair = count_fad_dff_violations(projected)

    n_pair_cells = len(
        df[df["table"] == "FAD"]
        .set_index(["country", "category", "date"])
        .index.intersection(df[df["table"] == "DFF"].set_index(["country", "category", "date"]).index)
    )
    report = pd.DataFrame(
        [
            {
                "constraint": "country_le_allcharg",
                "n_checked": int(df["country"].isin(OVERSUBSCRIBED).sum()),
                "violations_pre": pre_country,
                "violations_post": post_country,
            },
            {
                "constraint": "fad_le_dff",
                "n_checked": n_pair_cells,
                "violations_pre": pre_pair,
                "violations_post": post_pair,
            },
        ]
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report.to_csv(OUT_DIR / "cone_violations.csv", index=False)
    projected.to_csv(OUT_DIR / "web_forecasts_cone.csv", index=False)
    print(report.to_string(index=False))
    print(f"written {OUT_DIR.relative_to(ROOT)}/cone_violations.csv + web_forecasts_cone.csv")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Project forecasts onto the FAD<=DFF / country<=AllCharg cone (AL5)")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    args = ap.parse_args()
    run(args.input)


if __name__ == "__main__":
    main()
