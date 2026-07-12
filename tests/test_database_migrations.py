"""H1 — versioned, fail-closed migrations for the DuckDB warehouse.

Covers: the string/comment-aware SQL splitter (``;`` and ``--`` inside literals
no longer corrupt statements), the 001 baseline byte-pinned to schema.sql, the
ordered application with per-file sha256 bookkeeping, the immutable-history
gate (a tampered applied migration aborts WITHOUT replacing the previous live
database), a corrupt migration aborting the same way, and the degraded-build
policy (missing alias lineage / DV aborts unless --allow-degraded, which builds
but records etl_run.build_status='degraded').
"""

import hashlib
import shutil

import duckdb
import pytest

import pipeline.build_database as bd
from tests.dbfixtures import mini_dv, mini_panel, mini_raw_dir, mini_snapshots

# ─────────────────────────── SQL splitter ───────────────────────────


def test_splitter_respects_literals_and_comments():
    sql = (
        "CREATE TABLE x (a VARCHAR DEFAULT 'a;b--c');\n"
        "-- comment; with ; semicolons and 'quotes\n"
        "INSERT INTO x VALUES ('it''s; -- tricky');\n"
        "/* block; comment /* nested; */ still; */\n"
        'CREATE TABLE "weird;name--tbl" (b INTEGER);\n'
        "SELECT 1"
    )
    stmts = list(bd._statements(sql))
    assert len(stmts) == 4, stmts
    con = duckdb.connect(":memory:")
    for s in stmts:
        con.execute(s)
    # the ';' and '--' INSIDE the literal survived the split intact
    assert con.execute("SELECT a FROM x").fetchall() == [("it's; -- tricky",)]
    assert con.execute('SELECT count(*) FROM "weird;name--tbl"').fetchone()[0] == 0


def test_splitter_dollar_quotes():
    stmts = list(bd._statements("SELECT $tag$a;b--c$tag$ AS s; SELECT 2"))
    assert len(stmts) == 2
    assert duckdb.connect(":memory:").execute(stmts[0]).fetchone()[0] == "a;b--c"


def test_splitter_unterminated_literal_aborts():
    with pytest.raises(ValueError):
        list(bd._statements("SELECT 'oops"))


# ─────────────────────────── chain discovery ───────────────────────────


def test_baseline_migration_001_is_schema_sql_byte_identical():
    chain = bd.migrations()
    assert chain[0].version == 1
    assert chain[0].path.read_bytes() == bd.SCHEMA_PATH.read_bytes(), (
        "la migración 001 debe ser byte-idéntica a schema.sql (baseline pinned)"
    )
    assert chain[-1].version == bd.SCHEMA_VERSION


def test_migrations_reject_gaps_and_bad_names(tmp_path):
    gap = tmp_path / "gap"
    gap.mkdir()
    (gap / "001_a.sql").write_text("SELECT 1")
    (gap / "003_b.sql").write_text("SELECT 1")
    with pytest.raises(SystemExit):
        bd.migrations(gap)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "01_short.sql").write_text("SELECT 1")
    with pytest.raises(SystemExit):
        bd.migrations(bad)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit):
        bd.migrations(empty)


# ─────────────────────────── application + bookkeeping ───────────────────────────


def test_apply_records_versions_and_checksums_in_order(tmp_path):
    con = duckdb.connect(":memory:")
    summary = bd.build(
        con, mini_panel(), mini_dv(), raw_dir=mini_raw_dir(tmp_path), snapshots_dir=mini_snapshots(tmp_path)
    )
    rows = con.execute("SELECT version, checksum FROM schema_version ORDER BY version").fetchall()
    assert rows == [(m.version, m.sha256) for m in bd.migrations()]
    assert summary == {"build_status": "ok", "degradations": [], "schema_version": bd.SCHEMA_VERSION}
    # etl_run points at the applied head, under a real FK to schema_version
    assert con.execute("SELECT schema_version FROM etl_run").fetchone()[0] == bd.SCHEMA_VERSION


# ─────────────────────────── end-to-end: previous DB stays intact ───────────────


def _wire_paths(tmp_path, monkeypatch):
    """Point the module-level paths at a sandbox with fixture inputs, so main()
    runs end-to-end (tmp build -> verify -> os.replace) against scratch files."""
    panel_path = tmp_path / "panel.csv"
    mini_panel().to_csv(panel_path, index=False)
    dv_path = tmp_path / "dv.csv"
    mini_dv().to_csv(dv_path, index=False)
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    for m in bd.migrations():
        shutil.copy(m.path, migdir / m.path.name)
    monkeypatch.setattr(bd, "PANEL_PATH", panel_path)
    monkeypatch.setattr(bd, "DV_RANK_PATH", dv_path)
    monkeypatch.setattr(bd, "RAW_DIR", mini_raw_dir(tmp_path))
    monkeypatch.setattr(bd, "SNAPSHOTS_DIR", mini_snapshots(tmp_path))
    monkeypatch.setattr(bd, "MIGRATIONS_DIR", migdir)
    monkeypatch.setattr(bd, "DUCKDB_PATH", tmp_path / "test.duckdb")
    monkeypatch.setattr(bd, "PARQUET_PATH", tmp_path / "test.parquet")
    return migdir


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_tampered_applied_migration_aborts_and_preserves_live_db(tmp_path, monkeypatch):
    migdir = _wire_paths(tmp_path, monkeypatch)
    bd.main([])  # first build: records the applied checksums in the live DB
    db = tmp_path / "test.duckdb"
    before = _sha(db)
    two = migdir / "002_provenance_and_timestamps.sql"
    two.write_text(two.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="checksum"):
        bd.main([])
    assert _sha(db) == before, "la DB previa fue reemplazada pese al checksum inválido"


def test_deleted_applied_migration_aborts(tmp_path, monkeypatch):
    migdir = _wire_paths(tmp_path, monkeypatch)
    bd.main([])
    db_before = _sha(tmp_path / "test.duckdb")
    (migdir / "002_provenance_and_timestamps.sql").unlink()
    with pytest.raises(SystemExit):
        bd.main([])
    assert _sha(tmp_path / "test.duckdb") == db_before


def test_corrupt_new_migration_aborts_without_replacing(tmp_path, monkeypatch):
    migdir = _wire_paths(tmp_path, monkeypatch)
    bd.main([])
    db = tmp_path / "test.duckdb"
    before = _sha(db)
    (migdir / "003_broken.sql").write_text("CREATE TABLE (;", encoding="utf-8")
    with pytest.raises((duckdb.Error, SystemExit)):
        bd.main([])
    assert _sha(db) == before, "la DB previa fue reemplazada pese a la migración corrupta"
    # y el fingerprint lógico sigue siendo legible/el mismo almacén
    assert bd.content_fingerprint(db) == bd.content_fingerprint(db)


def test_migration_rollback_is_transactional(tmp_path):
    """A failing statement rolls back ITS migration inside the connection."""
    migdir = tmp_path / "m"
    migdir.mkdir()
    (migdir / "001_ok.sql").write_text("CREATE TABLE ok_t (a INTEGER);", encoding="utf-8")
    (migdir / "002_fails_midway.sql").write_text(
        "CREATE TABLE gone_t (a INTEGER);\nINSERT INTO nonexistent VALUES (1);", encoding="utf-8"
    )
    con = duckdb.connect(":memory:")
    with pytest.raises(duckdb.Error):
        bd._apply_migrations(con, bd.migrations(migdir))
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "ok_t" in tables, "la migración 001 (previa, completa) debe quedar aplicada"
    assert "gone_t" not in tables, "la migración 002 fallida debe quedar TODA revertida"


# ─────────────────────────── degraded-build policy ───────────────────────────


def test_dv_missing_aborts_by_default(tmp_path):
    with pytest.raises(SystemExit, match="DV ausente"):
        bd.build(duckdb.connect(":memory:"), mini_panel(), None, raw_dir=mini_raw_dir(tmp_path))


def test_dv_missing_with_flag_records_degraded(tmp_path):
    con = duckdb.connect(":memory:")
    summary = bd.build(
        con,
        mini_panel(),
        None,
        raw_dir=mini_raw_dir(tmp_path),
        snapshots_dir=mini_snapshots(tmp_path),
        allow_degraded=True,
    )
    assert summary["build_status"] == "degraded"
    status, reasons = con.execute("SELECT build_status, degradations FROM etl_run").fetchone()
    assert status == "degraded" and "dv_missing" in reasons
    assert con.execute("SELECT count(*) FROM fact_dv_rank").fetchone()[0] == 0


def test_alias_missing_aborts_by_default(tmp_path):
    raw = mini_raw_dir(tmp_path, with_raw_category=False)
    with pytest.raises(SystemExit, match="raw_category"):
        bd.build(duckdb.connect(":memory:"), mini_panel(), mini_dv(), raw_dir=raw)


def test_alias_missing_with_flag_records_degraded(tmp_path):
    raw = mini_raw_dir(tmp_path, with_raw_category=False)
    con = duckdb.connect(":memory:")
    summary = bd.build(
        con, mini_panel(), mini_dv(), raw_dir=raw, snapshots_dir=mini_snapshots(tmp_path), allow_degraded=True
    )
    assert summary["build_status"] == "degraded"
    status, reasons = con.execute("SELECT build_status, degradations FROM etl_run").fetchone()
    assert status == "degraded" and "alias_lineage_missing" in reasons
    assert con.execute("SELECT count(*) FROM dim_category_alias").fetchone()[0] == 0


def test_snapshots_missing_is_recorded_not_fatal(tmp_path):
    """Snapshots are gitignored/S3-mastered: a clean clone or CI may lack them,
    so the build proceeds but the degradation is RECORDED (never silent)."""
    con = duckdb.connect(":memory:")
    summary = bd.build(con, mini_panel(), mini_dv(), raw_dir=mini_raw_dir(tmp_path), snapshots_dir=None)
    assert summary["build_status"] == "degraded"
    assert "source_lineage_missing" in summary["degradations"]
    assert con.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == 0


def test_ok_build_has_no_degradations(tmp_path, monkeypatch):
    _wire_paths(tmp_path, monkeypatch)
    bd.main([])
    con = duckdb.connect(str(tmp_path / "test.duckdb"), read_only=True)
    try:
        status, reasons = con.execute("SELECT build_status, degradations FROM etl_run").fetchone()
        assert status == "ok" and reasons is None
        assert con.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == len(
            list((tmp_path / "snapshots").glob("*.html"))
        )
    finally:
        con.close()


def test_main_allow_degraded_flag_end_to_end(tmp_path, monkeypatch):
    _wire_paths(tmp_path, monkeypatch)
    (tmp_path / "dv.csv").unlink()  # DV desaparece: default aborta, flag construye degradado
    with pytest.raises(SystemExit):
        bd.main([])
    bd.main(["--allow-degraded"])
    con = duckdb.connect(str(tmp_path / "test.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT build_status FROM etl_run").fetchone()[0] == "degraded"
    finally:
        con.close()


def test_double_build_content_fingerprint_is_deterministic(tmp_path, monkeypatch):
    """Two consecutive builds from identical inputs: identical logical content
    (the .duckdb FILE differs only in bitácora wall-clock + storage order — the
    documented mechanism; the Parquet stays byte-identical)."""
    _wire_paths(tmp_path, monkeypatch)
    bd.main([])
    fp1 = bd.content_fingerprint(tmp_path / "test.duckdb")
    pq1 = _sha(tmp_path / "test.parquet")
    bd.main([])
    assert bd.content_fingerprint(tmp_path / "test.duckdb") == fp1
    assert _sha(tmp_path / "test.parquet") == pq1


def test_governance_note_no_stale_claims():
    """docs/data_dictionary.md ya no puede afirmar que 'no hay scripts de
    migración' — la política H1 los introdujo (guard documental barato)."""
    text = (bd.SCHEMA_PATH.parent / "docs" / "data_dictionary.md").read_text(encoding="utf-8")
    assert "ni scripts de migración" not in text
    assert "migraci" in text.lower()
