"""AL5 — retrospective cone audit: project a published forecast CSV onto the cone.

The projection itself is SINGLE-SOURCED in ``vp_model.cone`` (order constraints
FAD <= DFF and oversubscribed country <= all_chargeability; point projected via
isotonic min/max, bands SHIFTED with the point so their calibrated width is
preserved; residual violations counted honestly after both passes — see that
module's docstring for the full policy).

Since F1 the projection is WIRED into the web publisher
(``generate_web_forecasts.run()`` projects every vintage before serializing and
exposes ``cone_violations_pre``/``cone_violations_post`` in
``web_forecasts_meta.json``). This script remains as the retrospective AUDIT
tool: point it at any published/archived forecast CSV to measure and project it
after the fact (the AQ campaign measured 113+30 violations -> 0 with it).

Outputs:
  * ``reports/eval/cone_violations.csv``      — violations pre/post per constraint
  * ``reports/eval/web_forecasts_cone.csv``   — the projected forecasts (audit artifact)

Usage (from repo root):
    ante/bin/python experiments/apply_cone_constraints.py [--input reports/prospective/web_forecasts.csv]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

# vp_model está instalado editable en el venv principal (pip install -e .), igual
# que en el resto de scripts de experiments/ que lo importan sin hacks de sys.path.
from vp_model.cone import (
    OVERSUBSCRIBED,
    apply_country_cap,
    apply_fad_dff,
    count_country_violations,
    count_fad_dff_violations,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "reports" / "prospective" / "web_forecasts.csv"
OUT_DIR = ROOT / "reports" / "eval"


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
    ap = argparse.ArgumentParser(description="Retrospective audit: project a forecast CSV onto the cone (AL5)")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    args = ap.parse_args()
    run(args.input)


if __name__ == "__main__":
    main()
