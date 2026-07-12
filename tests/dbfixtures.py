"""Synthetic fixtures shared by the DuckDB warehouse tests
(test_database_migrations / test_provenance_chain / test_database_timestamps).

Everything is tiny but contract-complete: the panel satisfies the schema CHECKs
(days_since_base == datediff from the 1975 epoch, dates only under status 'F'),
the raw CSVs carry ``raw_category`` so the alias bridge loads, and the snapshot
filenames parse with ``vp_data.visa_common.extract_datetime_from_link``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

EPOCH = pd.Timestamp("1975-01-01")
START = "2020-01-01"
N_MONTHS = 30  # 2 categorías x 2 tablas x 30 meses = 120 filas (>=100 para el muestreo H2)


def mini_panel(n_months: int = N_MONTHS, start: str = START) -> pd.DataFrame:
    """Flat panel: mexico x {F1, EB1} x {FAD, DFF} x n months, all status 'F'."""
    months = pd.date_range(start, periods=n_months, freq="MS")
    rows = []
    for block, cat in (("family", "F1"), ("employment", "EB1")):
        for table in ("FAD", "DFF"):
            for i, m in enumerate(months):
                pdate = m - pd.DateOffset(years=5) + pd.Timedelta(days=i)
                rows.append(
                    {
                        "country": "mexico",
                        "block": block,
                        "category": cat,
                        "table": table,
                        "bulletin_date": m,
                        "status": "F",
                        "priority_date": pdate,
                        "days_since_base": (pdate - EPOCH).days,
                        "raw_value": pdate.strftime("%d%b%y").upper(),
                    }
                )
    return pd.DataFrame(rows)


def mini_dv(n_months: int = 3, start: str = START) -> pd.DataFrame:
    """Diversity-Visa rank frame in the shape scrape_dv writes."""
    months = pd.date_range(start, periods=n_months, freq="MS")
    return pd.DataFrame(
        {
            "region": ["africa"] * n_months,
            "visa_bulletin_date": months,
            "status": ["F"] * n_months,
            "rank_cutoff": [10000 + 100 * i for i in range(n_months)],
            "raw_value": [f"{10000 + 100 * i:,}" for i in range(n_months)],
            "exceptions": [None] * n_months,
        }
    )


def mini_raw_dir(tmp: Path, with_raw_category: bool = True) -> Path:
    """A raw per-country CSV dir for the alias bridge (or a pre-lineage one)."""
    raw = tmp / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        {
            "F_level": ["1", "1", "1"],
            "visa_bulletin_date": ["2020-01-01", "2020-02-01", "2020-03-01"],
        }
    )
    if with_raw_category:
        df["raw_category"] = ["F1st", "F1st", "Family 1st"]
    df.to_csv(raw / "mexico_family_visa_backlog_timecourse.csv", index=False)
    return raw


def mini_snapshots(tmp: Path, months: pd.DatetimeIndex | None = None) -> Path:
    """Frozen-HTML snapshot files whose names map to the panel's months."""
    if months is None:
        months = pd.date_range(START, periods=N_MONTHS, freq="MS")
    snaps = tmp / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    for m in months:
        name = f"visa-bulletin-for-{m.strftime('%B').lower()}-{m.year}.html"
        (snaps / name).write_text(f"<html>bulletin {m:%Y-%m}</html>", encoding="utf-8")
    return snaps
