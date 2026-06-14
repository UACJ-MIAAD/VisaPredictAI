"""
Consolidate the 10 per-country CSVs (employment + family) into the single long
panel  y_{p,c,b,t}  promised by the VisaPredict AI anteproyecto:

    p = país o área de cargabilidad
    c = categoría migratoria  (EB1..EB4 / F1,F2A,F2B,F3,F4)
    b = tipo de tabla         (FAD / DFF)
    t = mes del boletín

Dependent variable: `days_since_base` = días desde una fecha base fija
(`BASE`), calculado **únicamente sobre observaciones con status 'F'** (una
fecha de prioridad específica). Las celdas 'C' (Current) y 'U' (Unavailable)
se conservan como anotación descriptiva en `status` / `raw_value` pero **no**
son objetivo predictivo (formulación v5.1).

Run from the repo root (after the scrapers have written `status`/`raw_value`):
    ante/bin/python build_panel.py
Writes: data/visa_panel_long.csv
"""
from __future__ import annotations
import pandas as pd
from pathlib import Path

DATA = Path("data")
OUT = DATA / "visa_panel_long.csv"

# Fixed reference epoch for the dependent variable. Chosen before any observed
# priority date (earliest seen: 1979-11, Philippines F4 in the 2001 bulletins)
# so days_since_base is always non-negative and comparable across the panel.
BASE = pd.Timestamp("1975-01-01")

# Raw scraper country slug -> canonical panel label.
COUNTRIES = {
    "mexico": "mexico",
    "india": "india",
    "china": "china",
    "philippines": "philippines",
    "row": "all_chargeability",  # "All Chargeability Areas Except Those Listed"
}

PANEL_COLS = [
    "country", "block", "category", "table",
    "bulletin_date", "status", "priority_date", "days_since_base", "raw_value",
]


def _require(df: pd.DataFrame, fp: Path) -> None:
    missing = {"status", "raw_value", "table_type"} - set(df.columns)
    if missing:
        raise SystemExit(
            f"{fp} no tiene las columnas {missing}. "
            f"Re-ejecuta los scrapers (que ya emiten status/raw_value/table_type) antes de build_panel."
        )


TABLE_MAP = {"final_action": "FAD", "dates_for_filing": "DFF"}


def load_employment() -> pd.DataFrame:
    frames = []
    for slug, canon in COUNTRIES.items():
        fp = DATA / f"{slug}_visa_backlog_timecourse.csv"
        df = pd.read_csv(fp)
        _require(df, fp)
        df = df.rename(columns={"final_action_dates": "priority_date",
                                "visa_bulletin_date": "bulletin_date"})
        df["country"] = canon
        df["block"] = "employment"
        df["category"] = df["EB_level"].astype(str)  # ya es código canónico EB1..EB5_*
        df["table"] = df["table_type"].map(TABLE_MAP)  # FAD + DFF (DFF desde Oct-2015)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def load_family() -> pd.DataFrame:
    frames = []
    for slug, canon in COUNTRIES.items():
        fp = DATA / f"{slug}_family_visa_backlog_timecourse.csv"
        df = pd.read_csv(fp)
        _require(df, fp)
        df = df.rename(columns={"final_action_dates": "priority_date",
                                "visa_bulletin_date": "bulletin_date"})
        df["country"] = canon
        df["block"] = "family"
        df["category"] = "F" + df["F_level"].astype(str)
        df["table"] = df["table_type"].map(TABLE_MAP)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    panel = pd.concat([load_employment(), load_family()], ignore_index=True)

    # format='mixed': the source CSVs mix "YYYY-MM-DD" (employment) and
    # "YYYY-MM-DD HH:MM:SS" (family); a single inferred format would coerce the
    # minority to NaT, so parse each value on its own.
    panel["bulletin_date"] = pd.to_datetime(panel["bulletin_date"], errors="coerce", format="mixed")
    panel["priority_date"] = pd.to_datetime(panel["priority_date"], errors="coerce", format="mixed")

    # The dependent variable lives ONLY on status 'F'. For C/U/NA the priority
    # date carries no predictive meaning (C was flattened to the bulletin date
    # upstream), so null it out and leave days_since_base undefined.
    not_f = panel["status"] != "F"
    panel.loc[not_f, "priority_date"] = pd.NaT
    panel["days_since_base"] = (panel["priority_date"] - BASE).dt.days

    panel = panel[PANEL_COLS].sort_values(
        ["country", "block", "category", "table", "bulletin_date"]
    ).reset_index(drop=True)

    # Defensive: the same canonical category can appear twice in one bulletin
    # during a label transition (e.g. the May-2022 EB-5 'Unreserved' split).
    # Keep the first and report, so the panel key stays unique.
    key = ["country", "block", "category", "table", "bulletin_date"]
    dup = panel.duplicated(subset=key, keep="first")
    if dup.any():
        print(f"  aviso: {int(dup.sum())} filas duplicadas por clave colapsadas (keep=first)")
        panel = panel[~dup].reset_index(drop=True)

    panel.to_csv(OUT, index=False)

    # ---- summary -----------------------------------------------------------
    n_series = panel.groupby(["country", "block", "category", "table"]).ngroups
    print(f"✓ Panel escrito en {OUT}")
    print(f"  filas totales      : {len(panel):,}")
    print(f"  series (p×c×b)      : {n_series}")
    print(f"  rango temporal      : {panel.bulletin_date.min():%Y-%m} → {panel.bulletin_date.max():%Y-%m}")
    print("\n  filas por status:")
    print(panel["status"].value_counts().to_string())
    print("\n  filas por bloque × tabla:")
    print(panel.groupby(["block", "table"]).size().to_string())
    f = panel[panel.status == "F"]
    print(f"\n  objetivo entrenable (status F): {len(f):,} filas "
          f"({100*len(f)/len(panel):.0f}% del panel)")
    print(f"  days_since_base rango: [{f.days_since_base.min():.0f}, {f.days_since_base.max():.0f}] "
          f"(base = {BASE:%Y-%m-%d})")


if __name__ == "__main__":
    main()
