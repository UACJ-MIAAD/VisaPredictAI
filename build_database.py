"""Materialize the star-schema database from the flat panel.

Reads ``data/processed/visa_panel_long.csv`` and loads it into a normalized
DuckDB star schema (``schema.sql``) whose PK / FK / CHECK constraints enforce the
data contract on insert, then exports a typed Parquet copy of the panel view.
Both outputs are regenerated artifacts (gitignored); the open CSV stays the
versioned source of truth.

    ante/bin/python build_database.py
Writes: data/processed/visapredict.duckdb · data/processed/visa_panel_long.parquet
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pandas as pd

from config import DUCKDB_PATH, DV_RANK_PATH, PANEL_PATH, PARQUET_PATH

logger = logging.getLogger(__name__)
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Display names for the dimension rows (the panel stores only the slug/code).
AREA_NAMES = {
    "mexico": "México",
    "india": "India",
    "china": "China (mainland born)",
    "philippines": "Philippines",
    "all_chargeability": "All Chargeability Areas Except Those Listed",
}
TABLE_NAMES = {"FAD": "Final Action Dates", "DFF": "Dates for Filing"}
REGION_NAMES = {
    "africa": "Africa",
    "asia": "Asia",
    "europe": "Europe",
    "north_america": "North America (Bahamas)",
    "oceania": "Oceania",
    "south_america_caribbean": "South America and the Caribbean",
}


def _statements(sql: str):
    """Yield the executable statements of schema.sql.

    Strips ``--`` line comments first so a semicolon inside a comment (e.g. the
    ``y_{p,c,b,t};`` in a docstring) never splits a statement, then splits on
    the real statement terminators.
    """
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    for stmt in no_comments.split(";"):
        if stmt.strip():
            yield stmt


def _load_dv(con: duckdb.DuckDBPyConnection, dv: pd.DataFrame, dim_date: pd.DataFrame) -> None:
    """Load the Diversity-Visa region dimension and rank fact (region x month)."""
    d = dv.copy()
    d["bulletin_date"] = pd.to_datetime(d["visa_bulletin_date"])
    d["rank_cutoff"] = d["rank_cutoff"].astype("Int64")

    regions = sorted(d["region"].unique())
    dim_region = pd.DataFrame({"region_id": range(1, len(regions) + 1), "slug": regions})
    dim_region["name"] = dim_region["slug"].map(lambda s: REGION_NAMES.get(s, s))

    fact = d.merge(dim_region[["region_id", "slug"]], left_on="region", right_on="slug", validate="m:1").merge(
        dim_date[["date_id", "bulletin_date"]], on="bulletin_date", validate="m:1"
    )[["region_id", "date_id", "status", "rank_cutoff", "raw_value", "exceptions"]]

    con.register("v_dim_region", dim_region)
    con.register("v_fact_dv", fact)
    con.execute("INSERT INTO dim_region SELECT region_id, slug, name FROM v_dim_region")
    con.execute(
        "INSERT INTO fact_dv_rank SELECT region_id, date_id, status, "
        "CAST(rank_cutoff AS INTEGER), raw_value, exceptions FROM v_fact_dv"
    )


def build(con: duckdb.DuckDBPyConnection, panel: pd.DataFrame, dv: pd.DataFrame | None = None) -> None:
    """Create the star schema on ``con`` and load it from the long ``panel`` and
    the optional Diversity-Visa ``dv`` rank frame.

    Rows are inserted under the live PK/FK/CHECK constraints, so data that
    violates the contract raises here instead of producing a bad database.
    """
    df = panel.copy()
    df["bulletin_date"] = pd.to_datetime(df["bulletin_date"])
    df["priority_date"] = pd.to_datetime(df["priority_date"])
    df["days_since_base"] = df["days_since_base"].astype("Int64")
    df["raw_value"] = df["raw_value"].astype("string")

    # dim_area
    areas = sorted(df["country"].unique())
    dim_area = pd.DataFrame({"area_id": range(1, len(areas) + 1), "slug": areas})
    dim_area["name"] = dim_area["slug"].map(lambda s: AREA_NAMES.get(s, s))
    dim_area["is_residual_group"] = dim_area["slug"] == "all_chargeability"

    # dim_category
    dim_category = df[["block", "category"]].drop_duplicates().sort_values(["block", "category"]).reset_index(drop=True)
    dim_category.insert(0, "category_id", range(1, len(dim_category) + 1))
    dim_category = dim_category.rename(columns={"category": "code"})

    # dim_table
    tables = sorted(df["table"].unique())
    dim_table = pd.DataFrame({"table_id": range(1, len(tables) + 1), "code": tables})
    dim_table["name"] = dim_table["code"].map(lambda c: TABLE_NAMES.get(c, c))

    # dim_date — union of every bulletin month across the panel and DV.
    all_dates = set(df["bulletin_date"].unique())
    if dv is not None:
        all_dates |= set(pd.to_datetime(dv["visa_bulletin_date"]).unique())
    dates = pd.to_datetime(sorted(all_dates))
    dim_date = pd.DataFrame({"date_id": range(1, len(dates) + 1), "bulletin_date": dates})
    dim_date["year"] = dim_date["bulletin_date"].dt.year
    dim_date["month"] = dim_date["bulletin_date"].dt.month
    # U.S. federal fiscal year starts Oct 1 (per-country limits reset there).
    dim_date["us_fiscal_year"] = dim_date["year"] + (dim_date["month"] >= 10).astype(int)

    # Map every fact row to its surrogate keys (1:1 lookups, validated).
    fact = (
        df.merge(dim_area[["area_id", "slug"]], left_on="country", right_on="slug", validate="m:1")
        .merge(dim_category.rename(columns={"code": "category"}), on=["block", "category"], validate="m:1")
        .merge(dim_table[["table_id", "code"]], left_on="table", right_on="code", validate="m:1")
        .merge(dim_date[["date_id", "bulletin_date"]], on="bulletin_date", validate="m:1")
    )[["area_id", "category_id", "table_id", "date_id", "status", "priority_date", "days_since_base", "raw_value"]]

    # Apply the DDL (tables + constraints + view).
    for stmt in _statements(SCHEMA_PATH.read_text(encoding="utf-8")):
        con.execute(stmt)

    # Load parents before the fact so the FK checks pass.
    con.register("v_dim_area", dim_area)
    con.register("v_dim_category", dim_category)
    con.register("v_dim_table", dim_table)
    con.register("v_dim_date", dim_date)
    con.register("v_fact", fact)
    con.execute("INSERT INTO dim_area SELECT area_id, slug, name, is_residual_group FROM v_dim_area")
    con.execute("INSERT INTO dim_category SELECT category_id, block, code FROM v_dim_category")
    con.execute("INSERT INTO dim_table SELECT table_id, code, name FROM v_dim_table")
    con.execute(
        "INSERT INTO dim_date SELECT date_id, CAST(bulletin_date AS DATE), year, month, us_fiscal_year FROM v_dim_date"
    )
    con.execute(
        "INSERT INTO fact_priority SELECT area_id, category_id, table_id, date_id, status, "
        "CAST(priority_date AS DATE), CAST(days_since_base AS INTEGER), raw_value FROM v_fact"
    )

    if dv is not None:
        _load_dv(con, dv, dim_date)


def main() -> None:
    df = pd.read_csv(PANEL_PATH, parse_dates=["bulletin_date", "priority_date"])
    dv = pd.read_csv(DV_RANK_PATH, parse_dates=["visa_bulletin_date"]) if DV_RANK_PATH.exists() else None
    DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DUCKDB_PATH.unlink(missing_ok=True)  # clean rebuild (DuckDB would append otherwise)
    con = duckdb.connect(str(DUCKDB_PATH))
    try:
        build(con, df, dv)
        con.execute(f"COPY (SELECT * FROM v_panel_long) TO '{PARQUET_PATH.as_posix()}' (FORMAT parquet)")
        tables = ["dim_area", "dim_category", "dim_table", "dim_date", "fact_priority"]
        if dv is not None:
            tables += ["dim_region", "fact_dv_rank"]
        for tbl in tables:
            row = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()
            n = row[0] if row else 0
            logger.info(f"  {tbl:14s}: {n:>6,} filas")
        logger.info(f"DuckDB escrito en {DUCKDB_PATH}")
        logger.info(f"Parquet escrito en {PARQUET_PATH}")
    finally:
        con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
