"""H4 — deterministic warehouse timestamps.

created_at/updated_at are DERIVED FROM THE DATA (the row's bulletin month; on a
real content change, the panel vintage of the build that saw it) — never from
the wall clock. Covered here: insert semantics (created == updated == bulletin
month, UTC, TIMESTAMPTZ), no-op rebuild preserving both columns byte-identically
(carry-forward from the previous live DB), a real change via fixture keeping
created_at and advancing updated_at to the data vintage, the created <= updated
order, and mart_series_summary exposing last_modified_at.
"""

import duckdb
import pandas as pd
import pytest

import pipeline.build_database as bd
from tests.dbfixtures import mini_dv, mini_panel, mini_raw_dir

FACT_TS_SQL = (
    'SELECT a.slug AS country, c.block AS block, c.code AS category, t.code AS "table", '
    "d.bulletin_date AS bulletin_date, f.raw_value, f.created_at, f.updated_at "
    "FROM fact_priority f "
    "JOIN dim_area     a ON a.area_id     = f.area_id "
    "JOIN dim_category c ON c.category_id = f.category_id "
    "JOIN dim_table    t ON t.table_id    = f.table_id "
    "JOIN dim_date     d ON d.date_id     = f.date_id "
    'ORDER BY country, block, category, "table", bulletin_date'
)


def _fact_ts(db_path) -> pd.DataFrame:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute("SET TimeZone='UTC'")
        return con.execute(FACT_TS_SQL).fetchdf()
    finally:
        con.close()


def _build_file(path, panel, tmp_path, prev=None):
    con = duckdb.connect(str(path))
    try:
        bd.build(con, panel, mini_dv(), raw_dir=mini_raw_dir(tmp_path), prev_state=prev)
    finally:
        con.close()


@pytest.fixture
def mem(tmp_path):
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    bd.build(con, mini_panel(), mini_dv(), raw_dir=mini_raw_dir(tmp_path))
    return con


# ─────────────────────────── insert semantics ───────────────────────────


def test_insert_sets_created_equal_updated_from_bulletin_month(mem):
    df = mem.execute(FACT_TS_SQL).fetchdf()
    assert (df["created_at"] == df["updated_at"]).all()
    expected = pd.to_datetime(df["bulletin_date"]).dt.tz_localize("UTC")
    assert (df["created_at"] == expected).all(), "created_at debe SER el mes del boletín (dato), no un reloj"


def test_timestamps_are_timestamptz_utc(mem):
    for tbl in ("fact_priority", "fact_dv_rank", "dim_category_alias", "etl_run"):
        cols = {
            r[1]: r[2]
            for r in mem.execute(f"PRAGMA table_info('{tbl}')").fetchall()
            if r[1] in ("created_at", "updated_at", "built_at_utc", "started_at", "completed_at")
        }
        assert cols, tbl
        assert all(t == "TIMESTAMP WITH TIME ZONE" for t in cols.values()), (tbl, cols)
    # los valores llegan con zona (UTC): medianoche del mes del boletín
    val = mem.execute("SELECT created_at FROM fact_priority LIMIT 1").fetchone()[0]
    assert val.tzinfo is not None and val.utcoffset().total_seconds() == 0


def test_created_never_after_updated(mem):
    for tbl in ("fact_priority", "fact_dv_rank", "dim_category_alias", "source_artifact"):
        bad = mem.execute(f"SELECT count(*) FROM {tbl} WHERE created_at > updated_at").fetchone()[0]
        assert bad == 0, tbl


def test_alias_timestamps_mirror_envelope(mem):
    rows = mem.execute("SELECT created_at, valid_from, updated_at, valid_to FROM dim_category_alias").fetchall()
    assert rows
    for created, vfrom, updated, vto in rows:
        assert created.date() == vfrom and updated.date() == vto


def test_dv_timestamps_derive_from_bulletin_month(mem):
    rows = mem.execute(
        "SELECT d.bulletin_date, f.created_at, f.updated_at FROM fact_dv_rank f JOIN dim_date d USING (date_id)"
    ).fetchall()
    assert rows
    for bdate, created, updated in rows:
        assert created.date() == bdate and updated.date() == bdate


# ─────────────────────────── rebuild semantics ───────────────────────────


def test_noop_rebuild_preserves_both_columns_byte_identical(tmp_path):
    p1 = tmp_path / "one.duckdb"
    _build_file(p1, mini_panel(), tmp_path)
    prev = bd.previous_state(p1)
    assert prev is not None and len(prev.fact_priority) == 120
    p2 = tmp_path / "two.duckdb"
    _build_file(p2, mini_panel(), tmp_path, prev=prev)
    a, b = _fact_ts(p1), _fact_ts(p2)
    pd.testing.assert_frame_equal(a, b)  # byte-comparación de las columnas
    assert bd.content_fingerprint(p1) == bd.content_fingerprint(p2)


def test_real_change_keeps_created_and_advances_updated_to_vintage(tmp_path):
    p1 = tmp_path / "one.duckdb"
    _build_file(p1, mini_panel(), tmp_path)
    panel2 = mini_panel()
    mask = (
        (panel2["bulletin_date"] == panel2["bulletin_date"].min())
        & (panel2["table"] == "FAD")
        & (panel2["block"] == "family")
    )
    assert mask.sum() == 1
    panel2.loc[mask, "raw_value"] = "REVISED"
    p2 = tmp_path / "two.duckdb"
    _build_file(p2, panel2, tmp_path, prev=bd.previous_state(p1))

    df = _fact_ts(p2)
    changed = df[df["raw_value"] == "REVISED"]
    assert len(changed) == 1
    row = changed.iloc[0]
    first_month = pd.Timestamp(panel2["bulletin_date"].min(), tz="UTC")
    vintage = pd.Timestamp(panel2["bulletin_date"].max(), tz="UTC")
    # created_at se CONSERVA (primera aparición); updated_at avanza al VINTAGE
    # del corte que introdujo el cambio (max mes del panel), no a un reloj.
    assert row["created_at"] == first_month
    assert row["updated_at"] == vintage
    untouched = df[df["raw_value"] != "REVISED"]
    assert (untouched["created_at"] == untouched["updated_at"]).all()


def test_advanced_stamp_survives_the_next_noop_rebuild(tmp_path):
    p1 = tmp_path / "one.duckdb"
    _build_file(p1, mini_panel(), tmp_path)
    panel2 = mini_panel()
    panel2.loc[panel2.index[:1], "raw_value"] = "REVISED"
    p2 = tmp_path / "two.duckdb"
    _build_file(p2, panel2, tmp_path, prev=bd.previous_state(p1))
    p3 = tmp_path / "three.duckdb"
    _build_file(p3, panel2, tmp_path, prev=bd.previous_state(p2))
    pd.testing.assert_frame_equal(_fact_ts(p2), _fact_ts(p3))
    assert bd.content_fingerprint(p2) == bd.content_fingerprint(p3)


def test_previous_state_tolerates_missing_or_alien_db(tmp_path):
    assert bd.previous_state(tmp_path / "nope.duckdb") is None
    alien = tmp_path / "alien.duckdb"
    con = duckdb.connect(str(alien))
    con.execute("CREATE TABLE unrelated (a INTEGER)")
    con.close()
    assert bd.previous_state(alien) is None  # esquema ajeno => sin historia, sin explotar


# ─────────────────────────── marts ───────────────────────────


def test_mart_series_summary_exposes_last_modified_at(mem):
    diff = mem.execute(
        "SELECT count(*) FROM mart_series_summary m JOIN ("
        '  SELECT a.slug AS country, c.block AS block, c.code AS category, t.code AS "table", '
        "         max(f.updated_at) AS mx "
        "  FROM fact_priority f "
        "  JOIN dim_area a ON a.area_id = f.area_id "
        "  JOIN dim_category c ON c.category_id = f.category_id "
        "  JOIN dim_table t ON t.table_id = f.table_id "
        "  GROUP BY 1, 2, 3, 4) x "
        'ON x.country = m.country AND x.block = m.block AND x.category = m.category AND x."table" = m."table" '
        "WHERE x.mx != m.last_modified_at"
    ).fetchone()[0]
    assert diff == 0, "last_modified_at debe ser max(updated_at) por serie"
    assert mem.execute("SELECT count(*) FROM mart_series_summary WHERE last_modified_at IS NULL").fetchone()[0] == 0
