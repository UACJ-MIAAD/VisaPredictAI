"""Star-schema integrity (DuckDB).

Builds the normalized database from the committed panel in memory and checks two
things: (1) the v_panel_long view reproduces the flat CSV losslessly, and (2) the
PK/FK/CHECK constraints actually reject rows that violate the data contract — so
the schema, not just pytest, enforces it.
"""

import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from build_database import SCHEMA_PATH, _statements, build  # noqa: E402
from config import DV_RANK_PATH, PANEL_PATH  # noqa: E402


def _loaded():
    con = duckdb.connect(":memory:")
    df = pd.read_csv(PANEL_PATH, parse_dates=["bulletin_date", "priority_date"])
    dv = pd.read_csv(DV_RANK_PATH, parse_dates=["visa_bulletin_date"])
    build(con, df, dv)
    return con, df, dv


def _empty_schema():
    con = duckdb.connect(":memory:")
    for stmt in _statements(SCHEMA_PATH.read_text(encoding="utf-8")):
        con.execute(stmt)
    con.execute("INSERT INTO dim_area VALUES (1, 'mexico', 'México', false)")
    con.execute("INSERT INTO dim_category VALUES (1, 'employment', 'EB1')")
    con.execute("INSERT INTO dim_table VALUES (1, 'FAD', 'Final Action Dates')")
    con.execute("INSERT INTO dim_date VALUES (1, DATE '2020-01-01', 2020, 1, 2020)")
    con.execute("INSERT INTO dim_region VALUES (1, 'africa', 'Africa')")
    return con


def test_dimensions_loaded():
    con, _, _ = _loaded()
    assert con.execute("SELECT count(*) FROM dim_table").fetchone()[0] == 2
    # exactly one residual group ("All Chargeability"), never treated as a country
    assert con.execute("SELECT count(*) FROM dim_area WHERE is_residual_group").fetchone()[0] == 1


def test_fact_rowcount_matches_panel():
    con, df, _ = _loaded()
    assert con.execute("SELECT count(*) FROM fact_priority").fetchone()[0] == len(df)


def test_view_reproduces_panel_losslessly():
    con, _, _ = _loaded()
    con.execute(f"CREATE TEMP TABLE csv AS SELECT * FROM read_csv_auto('{PANEL_PATH.as_posix()}')")
    cols = 'country,block,category,"table",bulletin_date,status,priority_date,days_since_base,raw_value'
    only_view = con.execute(
        f"SELECT count(*) FROM (SELECT {cols} FROM v_panel_long EXCEPT SELECT {cols} FROM csv)"
    ).fetchone()[0]
    only_csv = con.execute(
        f"SELECT count(*) FROM (SELECT {cols} FROM csv EXCEPT SELECT {cols} FROM v_panel_long)"
    ).fetchone()[0]
    assert only_view == 0 and only_csv == 0, f"vista≠csv (solo_vista={only_view}, solo_csv={only_csv})"


def test_priority_date_not_after_bulletin():
    con, _, _ = _loaded()
    viol = con.execute(
        "SELECT count(*) FROM fact_priority f JOIN dim_date d USING (date_id) WHERE f.priority_date > d.bulletin_date"
    ).fetchone()[0]
    assert viol == 0, f"{viol} fechas de prioridad posteriores al boletín"


def test_valid_fact_row_inserts():
    con = _empty_schema()
    con.execute("INSERT INTO fact_priority VALUES (1, 1, 1, 1, 'F', DATE '2010-01-01', 12784, '01JAN10')")
    assert con.execute("SELECT count(*) FROM fact_priority").fetchone()[0] == 1


@pytest.mark.parametrize(
    "bad_row",
    [
        "(1, 1, 1, 1, 'BAD', NULL, NULL, 'x')",  # status outside the domain
        "(1, 1, 1, 1, 'C', NULL, 5, 'x')",  # days_since_base set but status != 'F'
        "(1, 1, 1, 1, 'F', NULL, NULL, 'x')",  # status 'F' without date/days
        "(1, 1, 1, 1, 'C', NULL, -3, 'x')",  # negative days
        "(9, 1, 1, 1, 'C', NULL, NULL, 'x')",  # FK to a nonexistent area
    ],
)
def test_constraints_reject_bad_rows(bad_row):
    con = _empty_schema()
    with pytest.raises(duckdb.ConstraintException):
        con.execute(f"INSERT INTO fact_priority VALUES {bad_row}")


# ─────────────────────────── Diversity Visa (DV) ───────────────────────────


def test_dv_loaded():
    con, _, dv = _loaded()
    assert con.execute("SELECT count(*) FROM dim_region").fetchone()[0] == 6
    assert con.execute("SELECT count(*) FROM fact_dv_rank").fetchone()[0] == len(dv)


def test_category_taxonomy_complete():
    # Pins the captured category taxonomy so a parser regression that silently
    # drops a category family fails the gate.
    con, _, _ = _loaded()
    codes = {r[0] for r in con.execute("SELECT code FROM dim_category").fetchall()}
    assert {"F1", "F2A", "F2B", "F3", "F4"} <= codes, "faltan categorías familiares"
    assert {"EB1", "EB2", "EB3", "EB4", "EB5"} <= codes, "faltan EB base"
    assert {"EB5_RURAL", "EB5_HIGHUNEMP", "EB5_INFRA"} <= codes, "faltan set-asides EB-5 post-2022"
    regions = {r[0] for r in con.execute("SELECT slug FROM dim_region").fetchall()}
    assert regions == {"africa", "asia", "europe", "north_america", "oceania", "south_america_caribbean"}


def test_dv_rank_defined_iff_F():
    con, _, _ = _loaded()
    bad = con.execute("SELECT count(*) FROM fact_dv_rank WHERE (status = 'F') != (rank_cutoff IS NOT NULL)").fetchone()[
        0
    ]
    assert bad == 0


def test_valid_dv_row_inserts():
    con = _empty_schema()
    con.execute("INSERT INTO fact_dv_rank VALUES (1, 1, 'F', 25000, '25,000', NULL)")
    assert con.execute("SELECT count(*) FROM fact_dv_rank").fetchone()[0] == 1


@pytest.mark.parametrize(
    "bad_row",
    [
        "(1, 1, 'BAD', NULL, 'x', NULL)",  # status outside the domain
        "(1, 1, 'C', 5, 'x', NULL)",  # rank_cutoff set but status != 'F'
        "(1, 1, 'F', NULL, 'x', NULL)",  # status 'F' without a rank
        "(1, 1, 'F', -3, 'x', NULL)",  # negative rank
        "(9, 1, 'C', NULL, 'x', NULL)",  # FK to a nonexistent region
    ],
)
def test_dv_constraints_reject_bad_rows(bad_row):
    con = _empty_schema()
    with pytest.raises(duckdb.ConstraintException):
        con.execute(f"INSERT INTO fact_dv_rank VALUES {bad_row}")
