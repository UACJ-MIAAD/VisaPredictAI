"""Materialize the star-schema database from the flat panel.

Reads ``data/processed/visa_panel_long.csv`` and loads it into a normalized
DuckDB star schema whose PK / FK / CHECK constraints enforce the data contract
on insert, then exports a typed Parquet copy of the panel view. Both outputs are
regenerated artifacts (gitignored); the open CSV stays the versioned source.

H1 — the DDL is a VERSIONED MIGRATION CHAIN: ``schema.sql`` is the baseline
(applied as migration 001, byte-pinned to ``pipeline/migrations/001_*.sql``)
and every later structural change is a numbered ``pipeline/migrations/NNN_*.sql``.
The chain is applied in order, each file inside its own transaction, into a
temp database that only replaces the live one on success (``os.replace``,
atomic) — a failed migration leaves the previous warehouse intact. Applied
versions are recorded in ``schema_version`` with each file's sha256; a checksum
mismatch against the previous live database aborts (history is immutable).
A build missing its lineage inputs (category aliases, DV) ABORTS unless
``--allow-degraded``, which builds but records ``etl_run.build_status='degraded'``.

H2 — ``etl_run`` carries the full identity of the build (git sha / dirty flag,
panel/dvc.lock/env-lock sha256, pipeline run id — passed via CLI/env, derived
from the real repo as fallback, NULL when honestly unavailable, never
fabricated); every fact row links back via ``etl_run_id``; ``source_artifact``
registers the frozen bulletin HTML (sha256 + vintage) behind every month.

H4 — ``created_at``/``updated_at`` on data tables are DERIVED FROM THE DATA
(the row's bulletin month; on content change, the build's panel vintage), never
wall-clock, so identical rebuilds are byte-identical at the content level.
Wall-clock lives only in the bitácora (``etl_run``,
``schema_version.applied_at``) — one reason the ``.duckdb`` FILE is not
byte-reproducible (DuckDB internal storage order is the other) and stays out of
DVC; the Parquet export and :func:`content_fingerprint` are the deterministic
contracts (see dvc.yaml, stage ``database``).

    ante/bin/python -m pipeline.build_database [--allow-degraded] [identity flags]
Writes: data/processed/visapredict.duckdb · data/processed/visa_panel_long.parquet
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from pipeline.db_governance import (
    _load_audit,
    _run_identity,
    content_fingerprint,
)
from pipeline.db_loaders import (
    AREA_NAMES,
    CATEGORY_META,
    REGION_NAMES,
    SOURCE_LICENSE,
    STATUS_META,
    TABLE_NAMES,
    PreviousState,
    _category_meta,
    _derive_timestamps,
    _fact_content_sha,
    _load_aliases,
    _load_dv,
    _sources_frame,
    previous_state,
)
from pipeline.db_migrations import (
    MIGRATIONS_DIR,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    Migration,
    _apply_migrations,
    _statements,
    migrations,
    verify_migration_history,
)
from vp_data.config import (
    DUCKDB_PATH,
    DV_RANK_PATH,
    PANEL_PATH,
    PARQUET_PATH,
    RAW_DIR,
    SNAPSHOTS_DIR,
)

logger = logging.getLogger(__name__)

# C6: el módulo se partió en `db_migrations`, `db_loaders` y `db_governance`, pero este
# sigue siendo el punto de entrada del stage DVC y la superficie que consumen las pruebas
# y el resto del pipeline. Los nombres se reexportan a propósito: partir un módulo no debe
# obligar a reescribir a sus consumidores.
__all__ = [
    "AREA_NAMES",
    "CATEGORY_META",
    "MIGRATIONS_DIR",
    "REGION_NAMES",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "SOURCE_LICENSE",
    "STATUS_META",
    "TABLE_NAMES",
    "Migration",
    "PreviousState",
    "_statements",
    "build",
    "content_fingerprint",
    "main",
    "migrations",
    "previous_state",
    "verify_migration_history",
]


# ─────────────────────────── migrations (H1) ───────────────────────────


# Latest structural version = head of the migration chain (baseline 001 folds
# the historical hand-bumped versions 1-3; 002 = provenance + timestamps).


# ─────────────────────────── deterministic timestamps (H4) ───────────────────────────


# ─────────────────────────── loaders ───────────────────────────


def build(
    con: duckdb.DuckDBPyConnection,
    panel: pd.DataFrame,
    dv: pd.DataFrame | None = None,
    *,
    raw_dir: Path | None = None,
    snapshots_dir: Path | None = None,
    run_info: dict | None = None,
    allow_degraded: bool = False,
    prev_state: PreviousState | None = None,
    migs: list[Migration] | None = None,
) -> dict:
    """Apply the migration chain on ``con`` and load it from the long ``panel``
    and the optional Diversity-Visa ``dv`` rank frame.

    Rows are inserted under the live PK/FK/CHECK constraints, so data that
    violates the contract raises here instead of producing a bad database.
    Missing lineage inputs (aliases, DV) ABORT unless ``allow_degraded``; every
    concession is recorded in ``etl_run.build_status``/``degradations``.
    Returns a summary: build_status, degradations, schema_version.
    """
    migs = list(migs) if migs is not None else migrations(MIGRATIONS_DIR)
    schema_ver = migs[-1].version
    degradations: list[str] = []

    if dv is None:
        # M4→H1: a 9-table warehouse used to print success on a missing DV
        # source; now the degraded build is a conscious, recorded decision.
        if not allow_degraded:
            raise SystemExit(
                f"DV ausente ({DV_RANK_PATH}): el almacén quedaría SIN datos Diversity Visa. "
                "Aborta (usa --allow-degraded para construir degradado)."
            )
        logger.warning(
            "DV ausente (%s): se construye SIN filas fact_dv_rank/dim_region — build degradado", DV_RANK_PATH
        )
        degradations.append("dv_missing")

    df = panel.copy().reset_index(drop=True)
    df["bulletin_date"] = pd.to_datetime(df["bulletin_date"])
    df["priority_date"] = pd.to_datetime(df["priority_date"])
    df["days_since_base"] = df["days_since_base"].astype("Int64")
    df["raw_value"] = df["raw_value"].astype("string")

    # dim_area
    areas = sorted(df["country"].unique())
    dim_area = pd.DataFrame({"area_id": range(1, len(areas) + 1), "slug": areas})
    dim_area["name"] = dim_area["slug"].map(lambda s: AREA_NAMES.get(s, s))
    dim_area["is_residual_group"] = dim_area["slug"] == "all_chargeability"

    # dim_category (+ hierarchy: parent_code, preference_level, is_subcategory, ina_basis)
    dim_category = df[["block", "category"]].drop_duplicates().sort_values(["block", "category"]).reset_index(drop=True)
    dim_category.insert(0, "category_id", range(1, len(dim_category) + 1))
    dim_category = dim_category.rename(columns={"category": "code"})
    meta = dim_category["code"].map(_category_meta)
    dim_category["parent_code"] = meta.map(lambda t: t[0])
    dim_category["preference_level"] = meta.map(lambda t: t[1])
    dim_category["is_subcategory"] = meta.map(lambda t: t[2])
    dim_category["ina_basis"] = meta.map(lambda t: t[3])

    # dim_status (reference dimension; only 'F' is a modeling target)
    dim_status = pd.DataFrame(STATUS_META, columns=["status", "label", "description", "is_predictable"])

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
    dim_date["quarter"] = dim_date["bulletin_date"].dt.quarter
    # U.S. federal fiscal year starts Oct 1 (per-country limits reset there).
    dim_date["us_fiscal_year"] = dim_date["year"] + (dim_date["month"] >= 10).astype(int)

    # H4: the build's data vintage = max bulletin month (updated_at advances to
    # THIS on content change — the cut that introduced the change, not a clock).
    ceiling = pd.Timestamp(dates.max()).tz_localize("UTC")

    # H2+H4: run linkage + derived row timestamps, on the NATURAL keys (surrogate
    # ids can renumber across rebuilds; the natural key is the stable identity).
    df["content_sha"] = _fact_content_sha(df)
    prev_fp = prev_state.fact_priority if prev_state is not None else None
    df["created_at"], df["updated_at"] = _derive_timestamps(
        df, ["country", "block", "category", "table", "bulletin_date"], prev_fp, ceiling
    )
    df["etl_run_id"] = 1

    # Map every fact row to its surrogate keys (1:1 lookups, validated).
    fact = (
        df.merge(dim_area[["area_id", "slug"]], left_on="country", right_on="slug", validate="m:1")
        .merge(dim_category.rename(columns={"code": "category"}), on=["block", "category"], validate="m:1")
        .merge(dim_table[["table_id", "code"]], left_on="table", right_on="code", validate="m:1")
        .merge(dim_date[["date_id", "bulletin_date"]], on="bulletin_date", validate="m:1")
    )[
        [
            "area_id",
            "category_id",
            "table_id",
            "date_id",
            "status",
            "priority_date",
            "days_since_base",
            "raw_value",
            "etl_run_id",
            "created_at",
            "updated_at",
        ]
    ]

    # Apply the DDL: the versioned migration chain (H1), each in a transaction.
    _apply_migrations(con, migs)

    # Load parents before the fact so the FK checks pass.
    con.register("v_dim_area", dim_area)
    con.register("v_dim_category", dim_category)
    con.register("v_dim_status", dim_status)
    con.register("v_dim_table", dim_table)
    con.register("v_dim_date", dim_date)
    con.register("v_fact", fact)
    con.execute("INSERT INTO dim_area SELECT area_id, slug, name, is_residual_group FROM v_dim_area")
    con.execute(
        "INSERT INTO dim_category SELECT category_id, block, code, parent_code, "
        "preference_level, is_subcategory, ina_basis FROM v_dim_category"
    )
    con.execute("INSERT INTO dim_status SELECT status, label, description, is_predictable FROM v_dim_status")
    con.execute("INSERT INTO dim_table SELECT table_id, code, name FROM v_dim_table")
    con.execute(
        "INSERT INTO dim_date SELECT date_id, CAST(bulletin_date AS DATE), year, month, quarter, "
        "us_fiscal_year FROM v_dim_date"
    )
    con.execute(
        "INSERT INTO fact_priority (area_id, category_id, table_id, date_id, status, priority_date, "
        "days_since_base, raw_value, etl_run_id, created_at, updated_at) "
        "SELECT area_id, category_id, table_id, date_id, status, CAST(priority_date AS DATE), "
        "CAST(days_since_base AS INTEGER), raw_value, etl_run_id, "
        "CAST(created_at AS TIMESTAMPTZ), CAST(updated_at AS TIMESTAMPTZ) FROM v_fact"
    )

    alias_degradation = _load_aliases(con, dim_category, Path(raw_dir) if raw_dir else RAW_DIR, allow_degraded)
    if alias_degradation:
        degradations.append(alias_degradation)

    sources = _sources_frame(Path(snapshots_dir) if snapshots_dir else None)
    if sources is None:
        # Snapshots are a gitignored, S3-mastered input: a clean clone / CI may
        # legitimately lack them, so this degradation WARNS and is recorded in
        # build_status instead of aborting (unlike alias/DV, which are in git).
        logger.warning(
            "sin snapshots (%s): source_artifact queda VACÍA — procedencia fila→fuente incompleta (build degradado)",
            snapshots_dir,
        )
        degradations.append("source_lineage_missing")
    else:
        con.register("v_source_artifact", sources)
        con.execute(
            "INSERT INTO source_artifact (source_id, filename, url, license, sha256, vintage, "
            "source_modified_at, created_at, updated_at) "
            "SELECT source_id, filename, url, license, sha256, CAST(vintage AS DATE), "
            "CAST(NULL AS TIMESTAMPTZ), CAST(created_at AS TIMESTAMPTZ), CAST(updated_at AS TIMESTAMPTZ) "
            "FROM v_source_artifact"
        )

    if dv is not None:
        _load_dv(con, dv, dim_date, prev_state.fact_dv if prev_state is not None else None, ceiling)

    build_status = "degraded" if degradations else "ok"
    _load_audit(con, schema_ver, run_info, build_status, degradations)

    # H2: etl_run_id carries no declarative FK (etl_run must land LAST as the
    # completeness sentinel — M2), so the linkage is asserted here, fail-closed.
    for tbl in ("fact_priority", "fact_dv_rank"):
        row = con.execute(f"SELECT count(*) FROM {tbl} WHERE etl_run_id NOT IN (SELECT run_id FROM etl_run)").fetchone()
        if row and row[0]:
            raise SystemExit(f"{row[0]} filas de {tbl} apuntan a un etl_run inexistente — enlace de procedencia roto")

    return {"build_status": build_status, "degradations": degradations, "schema_version": schema_ver}


# ─────────────────────────── determinism contract ───────────────────────────

# Wall-clock (bitácora) columns excluded from the logical fingerprint: they are
# the ONLY nondeterministic content in the warehouse, by design (H4).
_VOLATILE_COLUMNS = {
    "etl_run": {"built_at_utc", "started_at", "completed_at"},
    "schema_version": {"applied_at"},
}


# ─────────────────────────── CLI ───────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build the DuckDB star warehouse + Parquet export from the flat panel.")
    ap.add_argument(
        "--allow-degraded",
        action="store_true",
        help="build even when lineage inputs (category aliases, DV) are missing; "
        "the concession is recorded in etl_run.build_status='degraded'",
    )
    ap.add_argument("--git-sha", default=None, help="full 40-char commit sha (default: env VP_GIT_SHA, else derived)")
    ap.add_argument(
        "--git-dirty",
        choices=["true", "false"],
        default=None,
        help="worktree dirty flag (default: env VP_GIT_DIRTY, else derived)",
    )
    ap.add_argument("--pipeline-run-id", default=None, help="run id (default: VP_PIPELINE_RUN_ID/GITHUB_RUN_ID/local)")
    ap.add_argument("--dvc-lock", type=Path, default=Path("dvc.lock"), help="dvc.lock to hash into etl_run")
    ap.add_argument(
        "--env-lock",
        type=Path,
        default=None,
        help="environment lock to hash (default: env VP_ENV_LOCK, else locks/runtime.txt)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    started_at = datetime.now(UTC)
    df = pd.read_csv(PANEL_PATH, parse_dates=["bulletin_date", "priority_date"])
    dv = pd.read_csv(DV_RANK_PATH, parse_dates=["visa_bulletin_date"]) if DV_RANK_PATH.exists() else None
    migs = migrations(MIGRATIONS_DIR)
    if DUCKDB_PATH.exists():
        # H1 fail-closed: an edited/deleted APPLIED migration aborts before any build.
        verify_migration_history(DUCKDB_PATH, migs)
    # H4: carry updated_at forward from the previous live warehouse (no-op
    # rebuilds keep both timestamp columns byte-identical).
    prev = previous_state(DUCKDB_PATH)
    run_info = _run_identity(args, started_at, PANEL_PATH)
    DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # M1: build into temp names and os.replace on success. The old
    # unlink-then-build destroyed the good warehouse FIRST, so a crash mid-build
    # (Ctrl-C, OOM) left a partial DB that opens without complaint and an empty
    # mart the modeling layer iterates over "successfully". The temp names match
    # the gitignore patterns (*.duckdb / *.parquet) on purpose.
    tmp_db = DUCKDB_PATH.with_name("visapredict.tmp.duckdb")
    tmp_parquet = PARQUET_PATH.with_name("visa_panel_long.tmp.parquet")
    tmp_db.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp_db))
    try:
        summary = build(
            con,
            df,
            dv,
            raw_dir=RAW_DIR,
            snapshots_dir=SNAPSHOTS_DIR,
            run_info=run_info,
            allow_degraded=args.allow_degraded,
            prev_state=prev,
            migs=migs,
        )
        con.execute(
            # ORDER BY explícito: el determinismo byte-a-byte del Parquet (en que se apoya
            # dvc.yaml) no debe depender del orden de escaneo interno de DuckDB (H2)
            f'COPY (SELECT * FROM v_panel_long ORDER BY country, block, category, "table", bulletin_date) '
            f"TO '{tmp_parquet.as_posix()}' (FORMAT parquet)"
        )
        # M3: re-read what was written — a COPY truncated by a full disk used to
        # leave a stale/corrupt parquet next to a fresh .duckdb, silently.
        row = con.execute(f"SELECT count(*) FROM read_parquet('{tmp_parquet.as_posix()}')").fetchone()
        n_parquet = row[0] if row else 0
        row = con.execute("SELECT count(*) FROM fact_priority").fetchone()
        n_fact = row[0] if row else 0
        if n_parquet != n_fact:
            raise SystemExit(f"Parquet truncado: {n_parquet} filas vs {n_fact} en fact_priority")
        tables = [
            "dim_area",
            "dim_category",
            "dim_category_alias",
            "dim_status",
            "dim_table",
            "dim_date",
            "dim_region",
            "fact_priority",
            "fact_dv_rank",
            "source_artifact",
            "schema_version",
            "etl_run",
        ]
        for tbl in tables:
            row = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()
            n = row[0] if row else 0
            logger.info(f"  {tbl:18s}: {n:>6,} filas")
    finally:
        con.close()
    # M1: only a FULLY built and verified pair replaces the live files (atomic).
    os.replace(tmp_db, DUCKDB_PATH)
    os.replace(tmp_parquet, PARQUET_PATH)
    logger.info(f"DuckDB escrito en {DUCKDB_PATH}")
    logger.info(f"Parquet escrito en {PARQUET_PATH}")
    logger.info(
        "build %s (schema v%d)%s",
        summary["build_status"],
        summary["schema_version"],
        f" — degradaciones: {', '.join(summary['degradations'])}" if summary["degradations"] else "",
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
