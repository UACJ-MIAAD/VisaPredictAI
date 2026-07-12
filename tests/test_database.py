"""Star-schema integrity (DuckDB).

Builds the normalized database from the committed panel in memory and checks two
things: (1) the v_panel_long view reproduces the flat CSV losslessly, and (2) the
PK/FK/CHECK constraints actually reject rows that violate the data contract — so
the schema, not just pytest, enforces it.
"""

from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent

from pipeline.build_database import SCHEMA_PATH, SCHEMA_VERSION, _statements, build  # noqa: E402
from vp_data.config import DV_RANK_PATH, PANEL_PATH  # noqa: E402


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
    con.execute("INSERT INTO dim_category VALUES (1, 'employment', 'EB1', NULL, 1, false, 'INA 203(b)(1)')")
    con.execute(
        "INSERT INTO dim_status VALUES "
        "('F','Final','x',true),('C','Current','x',false),('U','Unavailable','x',false),('UNK','Unknown','x',false)"
    )
    con.execute("INSERT INTO dim_table VALUES (1, 'FAD', 'Final Action Dates')")
    con.execute("INSERT INTO dim_date VALUES (1, DATE '2020-01-01', 2020, 1, 1, 2020)")
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


def test_dv_view_reproduces_source_losslessly():
    # M3: the count-only check let a column swap in _load_dv pass with 1,647
    # correct rows of garbage. Same EXCEPT round-trip the panel view gets.
    con, _, _ = _loaded()
    con.execute(f"CREATE TEMP TABLE dvcsv AS SELECT * FROM read_csv_auto('{DV_RANK_PATH.as_posix()}')")
    view_cols = "region, rank_cutoff, status, raw_value, exceptions, bulletin_date"
    csv_cols = "region, rank_cutoff, status, raw_value, exceptions, visa_bulletin_date"
    only_view = con.execute(
        f"SELECT count(*) FROM (SELECT {view_cols} FROM v_dv_long EXCEPT SELECT {csv_cols} FROM dvcsv)"
    ).fetchone()[0]
    only_csv = con.execute(
        f"SELECT count(*) FROM (SELECT {csv_cols} FROM dvcsv EXCEPT SELECT {view_cols} FROM v_dv_long)"
    ).fetchone()[0]
    assert only_view == 0 and only_csv == 0, f"v_dv_long≠csv (solo_vista={only_view}, solo_csv={only_csv})"


def test_fact_priority_rejects_duplicate_pk():
    # M6 gap: the alias PK had a rejection test but the MAIN fact didn't.
    con, _, _ = _loaded()
    row = con.execute("SELECT area_id, category_id, table_id, date_id FROM fact_priority LIMIT 1").fetchone()
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            # H2/H4: incluye las columnas de procedencia para que el rechazo sea
            # genuinamente por PK duplicada, no por un NOT NULL colateral.
            "INSERT INTO fact_priority (area_id, category_id, table_id, date_id, status, priority_date, "
            "days_since_base, raw_value, etl_run_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'U', NULL, NULL, 'dup', 1, "
            "TIMESTAMPTZ '2020-01-01 00:00:00+00', TIMESTAMPTZ '2020-01-01 00:00:00+00')",
            list(row),
        )


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


# ─────────────────────────── Phase 2: hierarchy & reference dims ───────────


def test_category_hierarchy():
    con, _, _ = _loaded()
    rows = dict(con.execute("SELECT code, parent_code FROM dim_category").fetchall())
    assert rows["EB5_RURAL"] == "EB5", "EB5_RURAL debe colgar de EB5"
    assert rows["EB3_OW"] == "EB3" and rows["EB4_RW"] == "EB4"
    assert rows["F2A"] == "F2" and rows["EB1"] is None
    # preference level + subcategory flag
    pref, sub = con.execute(
        "SELECT preference_level, is_subcategory FROM dim_category WHERE code='EB5_INFRA'"
    ).fetchone()
    assert pref == 5 and sub is True
    assert con.execute("SELECT preference_level FROM dim_category WHERE code='F4'").fetchone()[0] == 4


def test_dim_status_reference():
    con, _, _ = _loaded()
    assert con.execute("SELECT count(*) FROM dim_status").fetchone()[0] == 4
    pred = {r[0] for r in con.execute("SELECT status FROM dim_status WHERE is_predictable").fetchall()}
    assert pred == {"F"}, "solo 'F' es objetivo predictivo"


def test_rollup_matches_fact():
    # the preference roll-up must account for every trainable ('F') observation
    con, _, _ = _loaded()
    rolled = con.execute("SELECT sum(n_obs) FROM v_trainable_by_preference").fetchone()[0]
    direct = con.execute("SELECT count(*) FROM fact_priority WHERE status='F'").fetchone()[0]
    assert rolled == direct, f"roll-up {rolled} ≠ F directo {direct}"


def test_status_fk_rejects_unknown():
    con = _empty_schema()
    con.execute("DELETE FROM dim_status WHERE status='C'")  # remove a status
    with pytest.raises(duckdb.ConstraintException):  # fact referencing it now fails the FK
        con.execute("INSERT INTO fact_priority VALUES (1, 1, 1, 1, 'C', NULL, NULL, 'x')")


# ─────────────────────────── Phase 2: category-alias lineage bridge ─────────


def test_alias_bridge_documents_drift():
    con, _, _ = _loaded()
    n = con.execute("SELECT count(*) FROM dim_category_alias").fetchone()[0]
    assert n > 21, "debe haber más alias que categorías (deriva de etiquetas)"
    # a category that drifted across many published spellings
    tea = con.execute("SELECT count(*) FROM v_category_alias WHERE canonical = 'EB5_TEA'").fetchone()[0]
    assert tea >= 2, "EB5_TEA debería tener varias etiquetas crudas observadas"
    # every alias resolves to a real canonical (the join loses nothing)
    orphans = con.execute(
        "SELECT count(*) FROM dim_category_alias x LEFT JOIN dim_category c USING (category_id) WHERE c.code IS NULL"
    ).fetchone()[0]
    assert orphans == 0


@pytest.mark.parametrize(
    "bad",
    [
        "(2, 99, 'x', DATE '2001-12-01', DATE '2002-01-01', 1)",  # FK to a nonexistent category
        "(2, 1, 'y', DATE '2020-01-01', DATE '2001-12-01', 1)",  # valid_from > valid_to
        "(2, 1, 'z', DATE '2001-12-01', DATE '2002-01-01', 0)",  # n_months not > 0
        "(2, 1, '1st', DATE '2001-12-01', DATE '2002-01-01', 1)",  # duplicate (category_id, raw_label)
    ],
)
def test_alias_constraints_reject_bad_rows(bad):
    con = _empty_schema()
    con.execute("INSERT INTO dim_category_alias VALUES (1, 1, '1st', DATE '2001-12-01', DATE '2020-01-01', 50)")
    with pytest.raises(duckdb.ConstraintException):
        con.execute(f"INSERT INTO dim_category_alias VALUES {bad}")


# ─────────────────────────── Phase 3: governance & marts ───────────────────


def test_governance_etl_run():
    con, _, _ = _loaded()
    # H1: schema_version guarda una fila POR MIGRACIÓN aplicada; la versión
    # estructural vigente es la cabeza de la cadena (max), no una fila única.
    assert con.execute("SELECT max(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert con.execute("SELECT count(*) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    n_fp, n_f, pct = con.execute("SELECT n_fact_priority, n_trainable_f, pct_trainable FROM etl_run").fetchone()
    assert n_fp == con.execute("SELECT count(*) FROM fact_priority").fetchone()[0]
    assert 0 <= pct <= 1 and n_f <= n_fp


def test_mart_training_f_is_clean():
    con, _, _ = _loaded()
    n = con.execute("SELECT count(*) FROM mart_training_F").fetchone()[0]
    assert n == con.execute("SELECT count(*) FROM fact_priority WHERE status='F'").fetchone()[0]
    # the dependent variable is never null in the training mart
    nulls = con.execute("SELECT count(*) FROM mart_training_F WHERE days_since_base IS NULL").fetchone()[0]
    assert nulls == 0


def test_mart_series_summary_covers_every_series():
    con, _, _ = _loaded()
    n_series = con.execute("SELECT count(*) FROM mart_series_summary").fetchone()[0]
    direct = con.execute(
        "SELECT count(*) FROM (SELECT DISTINCT area_id, category_id, table_id FROM fact_priority)"
    ).fetchone()[0]
    assert n_series == direct
