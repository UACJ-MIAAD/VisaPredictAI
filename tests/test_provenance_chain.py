"""H2 — provenance chain: every fact row resolves fila → fuente → run.

Builds the warehouse from synthetic fixtures (>=100 fact rows, one frozen
snapshot per bulletin month) and executes the audit query EXACTLY as documented
in docs/manual_conexion_duckdb.md (extracted from the doc, so the documentation
can never drift from what actually runs). Verifies on a 100-row sample that the
chain is complete: source filename + sha256 (recomputed from disk), the S3
archival URL, and the etl_run identity passed via run_info (CLI/env).
"""

import hashlib
import re
from pathlib import Path

import duckdb
import pandas as pd

import pipeline.build_database as bd
from tests.dbfixtures import mini_dv, mini_panel, mini_raw_dir, mini_snapshots

MANUAL = Path(__file__).resolve().parents[1] / "docs" / "manual_conexion_duckdb.md"

RUN_INFO = {
    "pipeline_run_id": "test-run-7",
    "git_sha": "a" * 40,
    "git_dirty": False,
    "panel_sha256": "b" * 64,
    "dvc_lock_sha256": "c" * 64,
    "env_lock_sha256": "d" * 64,
    "started_at": pd.Timestamp("2026-07-12T00:00:00Z").to_pydatetime(),
}


def documented_audit_query() -> str:
    """The query BETWEEN the markers in the manual — the doc is the source."""
    text = MANUAL.read_text(encoding="utf-8")
    m = re.search(r"<!-- provenance-audit-query:begin -->.*?```sql\n(.*?)```", text, re.S)
    assert m, "la query de auditoría de procedencia desapareció del manual (markers)"
    return m.group(1)


def _built(tmp_path) -> tuple[duckdb.DuckDBPyConnection, Path]:
    snaps = mini_snapshots(tmp_path)
    con = duckdb.connect(":memory:")
    summary = bd.build(
        con, mini_panel(), mini_dv(), raw_dir=mini_raw_dir(tmp_path), snapshots_dir=snaps, run_info=RUN_INFO
    )
    assert summary["build_status"] == "ok"
    return con, snaps


def test_chain_complete_on_100_sampled_rows(tmp_path):
    con, snaps = _built(tmp_path)
    rows = con.execute(documented_audit_query()).fetchdf()
    assert len(rows) >= 100, "el fixture debe rendir >=100 filas para el muestreo"
    sample = rows.sample(n=100, random_state=7)
    # fuente: cada fila muestreada resuelve a su snapshot congelado
    assert sample["source_file"].notna().all()
    assert sample["source_sha256"].notna().all()
    assert sample["source_url"].str.startswith("s3://").all()
    # run: cada fila enlaza la corrida y su identidad completa
    assert (sample["run_id"] == 1).all()
    assert (sample["git_sha"] == RUN_INFO["git_sha"]).all()
    assert (sample["pipeline_run_id"] == RUN_INFO["pipeline_run_id"]).all()
    assert (sample["build_status"] == "ok").all()


def test_recorded_sha256_matches_artifact_on_disk(tmp_path):
    con, snaps = _built(tmp_path)
    for filename, recorded in con.execute("SELECT filename, sha256 FROM source_artifact LIMIT 5").fetchall():
        disk = hashlib.sha256((snaps / filename).read_bytes()).hexdigest()
        assert recorded == disk, f"sha256 registrado != artefacto real ({filename})"


def test_every_bulletin_month_has_a_source(tmp_path):
    con, _ = _built(tmp_path)
    orphan_months = con.execute(
        "SELECT count(*) FROM dim_date d LEFT JOIN source_artifact s ON s.vintage = d.bulletin_date "
        "WHERE s.source_id IS NULL"
    ).fetchone()[0]
    assert orphan_months == 0
    assert con.execute("SELECT count(*) FROM source_artifact").fetchone()[0] == len(
        list(mini_panel()["bulletin_date"].unique())
    )


def test_source_metadata_fields(tmp_path):
    con, _ = _built(tmp_path)
    url, lic, vintage, modified = con.execute(
        "SELECT url, license, vintage, source_modified_at FROM source_artifact ORDER BY source_id LIMIT 1"
    ).fetchone()
    assert url.startswith("s3://visapredictai-raw-snapshots/raw-html/")
    assert "public domain" in lic
    assert vintage is not None
    assert modified is None, "source_modified_at no se rastrea: NULL honesto, jamás fabricado"


def test_identity_absent_is_honest_null(tmp_path):
    con = duckdb.connect(":memory:")
    bd.build(
        con,
        mini_panel(),
        mini_dv(),
        raw_dir=mini_raw_dir(tmp_path),
        snapshots_dir=mini_snapshots(tmp_path),
        run_info=None,
    )
    row = con.execute(
        "SELECT pipeline_run_id, git_sha, git_dirty, panel_sha256, dvc_lock_sha256, env_lock_sha256, started_at "
        "FROM etl_run"
    ).fetchone()
    assert row == (None,) * 7, "identidad no disponible => NULL, nunca inventada"


def test_facts_link_the_single_run(tmp_path):
    con, _ = _built(tmp_path)
    for tbl in ("fact_priority", "fact_dv_rank"):
        distinct = con.execute(f"SELECT DISTINCT etl_run_id FROM {tbl}").fetchall()
        assert distinct == [(1,)]
    # y el run enlaza la versión de esquema aplicada (FK real)
    assert con.execute("SELECT schema_version FROM etl_run").fetchone()[0] == bd.SCHEMA_VERSION
