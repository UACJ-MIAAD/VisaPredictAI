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
import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from vp_data.config import (
    DUCKDB_PATH,
    DV_RANK_PATH,
    PANEL_PATH,
    PARQUET_PATH,
    RAW_DIR,
    SNAPSHOTS_DIR,
    SNAPSHOTS_S3_PREFIX,
)

logger = logging.getLogger(__name__)
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# Visa Bulletin content is a U.S. federal government work (no copyright).
SOURCE_LICENSE = "U.S. Government work - public domain (17 U.S.C. 105)"
# Canonical NULL token for row-content hashing (can't appear in real cells).
_NULL_TOKEN = "\x00"
_MIGRATION_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$")

# Display names for the dimension rows (the panel stores only the slug/code).
AREA_NAMES = {
    "mexico": "México",
    "india": "India",
    "china": "China (mainland born)",
    "philippines": "Philippines",
    "all_chargeability": "All Chargeability Areas Except Those Listed",
}
TABLE_NAMES = {"FAD": "Final Action Dates", "DFF": "Dates for Filing"}
CATEGORY_META = {
    # code: (parent_code, preference_level, is_subcategory, ina_basis)
    "F1": (None, 1, False, "INA 203(a)(1)"),
    "F2A": ("F2", 2, True, "INA 203(a)(2)(A)"),
    "F2B": ("F2", 2, True, "INA 203(a)(2)(B)"),
    "F3": (None, 3, False, "INA 203(a)(3)"),
    "F4": (None, 4, False, "INA 203(a)(4)"),
    "EB1": (None, 1, False, "INA 203(b)(1)"),
    "EB2": (None, 2, False, "INA 203(b)(2)"),
    "EB3": (None, 3, False, "INA 203(b)(3)"),
    "EB3_OW": ("EB3", 3, True, "INA 203(b)(3)"),  # Other Workers
    "EB4": (None, 4, False, "INA 203(b)(4)"),
    "EB4_RW": ("EB4", 4, True, "INA 203(b)(4)"),  # Certain Religious Workers
    "EB4_TRANS": ("EB4", 4, True, "INA 203(b)(4)"),  # Iraqi/Afghan Translators
    "EB5": (None, 5, False, "INA 203(b)(5)"),
    "EB5_TEA": ("EB5", 5, True, "INA 203(b)(5)"),  # Targeted Employment Area
    "EB5_PILOT": ("EB5", 5, True, "INA 203(b)(5)"),  # Regional Center Pilot
    "EB5_RC": ("EB5", 5, True, "INA 203(b)(5)"),  # Regional Center
    "EB5_NONRC": ("EB5", 5, True, "INA 203(b)(5)"),  # Non-Regional Center
    "EB5_UNRESERVED": ("EB5", 5, True, "INA 203(b)(5)"),
    "EB5_RURAL": ("EB5", 5, True, "INA 203(b)(5)(B)(ii)"),  # RIA-2022 set-asides
    "EB5_HIGHUNEMP": ("EB5", 5, True, "INA 203(b)(5)(B)(ii)"),
    "EB5_INFRA": ("EB5", 5, True, "INA 203(b)(5)(B)(ii)"),
}
# C/F/U/UNK regime, promoted to dim_status. Only 'F' is a modeling target.
STATUS_META = [
    ("F", "Final", "Se publicó una fecha o rango específico (único objetivo predictivo).", True),
    ("C", "Current", "Categoría al día ese mes (sin backlog).", False),
    ("U", "Unavailable", "Sin números disponibles ese mes.", False),
    ("UNK", "Unknown", "Celda vacía o no parseable.", False),
]
REGION_NAMES = {
    "africa": "Africa",
    "asia": "Asia",
    "europe": "Europe",
    "north_america": "North America (Bahamas)",
    "oceania": "Oceania",
    "south_america_caribbean": "South America and the Caribbean",
}


# ─────────────────────────── migrations (H1) ───────────────────────────


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    path: Path
    sha256: str


def migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Discover the migration chain: ``NNN_descripcion.sql``, contiguous from 001.

    Anything unparseable, duplicated or with a gap aborts — a hole in the chain
    means an edited history, and history is immutable (checksums pin the rest).
    """
    mig_dir = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR
    found: dict[int, Migration] = {}
    for fp in sorted(mig_dir.glob("*.sql")):
        m = _MIGRATION_NAME.match(fp.name)
        if not m:
            raise SystemExit(f"migración con nombre inválido: {fp.name} (esperado NNN_descripcion.sql)")
        version = int(m.group(1))
        if version in found:
            raise SystemExit(f"versión de migración duplicada: {version:03d}")
        found[version] = Migration(version, m.group(2), fp, hashlib.sha256(fp.read_bytes()).hexdigest())
    if not found:
        raise SystemExit(f"sin migraciones en {mig_dir}")
    chain = [found[v] for v in sorted(found)]
    if [m.version for m in chain] != list(range(1, len(chain) + 1)):
        raise SystemExit(f"cadena de migraciones no contigua: {sorted(found)} (esperado 1..{len(chain)})")
    return chain


# Latest structural version = head of the migration chain (baseline 001 folds
# the historical hand-bumped versions 1-3; 002 = provenance + timestamps).
SCHEMA_VERSION = migrations()[-1].version


def _split_sql(sql: str) -> list[str]:
    """Split a SQL script into statements, respecting strings and comments.

    Replaces the old naive comment-strip + ``split(';')`` (M6): a ``;`` or
    ``--`` inside a single-quoted literal, double-quoted identifier, dollar
    quote or (nested) block comment no longer corrupts statements. Comments are
    dropped; statements come back stripped and non-empty.
    """
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    def _flush() -> None:
        stmt = "".join(buf).strip()
        if stmt:
            stmts.append(stmt)
        buf.clear()

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch in ("'", '"'):  # string literal / quoted identifier ('' and "" escape)
            quote, start = ch, i
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ValueError(f"literal sin cerrar ({quote}) en el SQL, posición {start}")
            buf.append(sql[start:i])
            continue
        if ch == "-" and nxt == "-":  # line comment
            while i < n and sql[i] != "\n":
                i += 1
            buf.append("\n")  # keep the break so tokens never glue together
            continue
        if ch == "/" and nxt == "*":  # block comment (Postgres-style nesting)
            depth, i = 1, i + 2
            while i < n and depth:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth, i = depth + 1, i + 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise ValueError("comentario de bloque sin cerrar en el SQL")
            buf.append(" ")
            continue
        if ch == "$":  # dollar-quoted string ($tag$ ... $tag$)
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group()
                end = sql.find(tag, m.end())
                if end == -1:
                    raise ValueError(f"dollar-quote sin cerrar ({tag}) en el SQL")
                buf.append(sql[i : end + len(tag)])
                i = end + len(tag)
                continue
        if ch == ";":
            _flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    _flush()
    return stmts


def _statements(sql: str) -> Iterator[str]:
    """Yield the executable statements of a SQL script (string/comment-aware)."""
    yield from _split_sql(sql)


def _apply_migrations(con: duckdb.DuckDBPyConnection, migs: list[Migration]) -> None:
    """Apply the chain in order, each migration inside its own transaction, then
    record every applied version in schema_version with its file checksum.

    The rollback story is layered: a failing statement rolls back ITS migration,
    and because the whole build happens in a temp file that only replaces the
    live database via os.replace on success, the previous warehouse stays intact.
    """
    for mig in migs:
        con.execute("BEGIN TRANSACTION")
        try:
            for stmt in _split_sql(mig.path.read_text(encoding="utf-8")):
                con.execute(stmt)
            con.execute("COMMIT")
        except Exception:  # amplio a propósito -- ROLLBACK garantizado ante CUALQUIER fallo, luego re-raise
            con.execute("ROLLBACK")
            logger.error("migración %03d (%s) FALLÓ — almacén previo intacto", mig.version, mig.description)
            raise
    applied_at = datetime.now(UTC)  # bitácora: wall-clock permitido aquí, jamás en hechos
    for mig in migs:
        con.execute(
            "INSERT INTO schema_version (version, description, applied_at, checksum) VALUES (?, ?, ?, ?)",
            [mig.version, mig.description, applied_at, mig.sha256],
        )


def verify_migration_history(prev_db_path: str | Path, migs: list[Migration]) -> None:
    """Fail-closed gate: every migration recorded in the previous live database
    must still exist as a file with the SAME sha256. An edited or deleted
    applied migration aborts the build (exit != 0) before touching anything.

    Tolerant only to what must be tolerated: a pre-migration warehouse (old
    schema_version without checksum column) or an unreadable file skip the gate
    with a warning — there is no recorded history to defend yet.
    """
    recorded: dict[int, str] = {}
    try:
        con = duckdb.connect(str(prev_db_path), read_only=True)
    except duckdb.Error as exc:
        logger.warning("almacén previo ilegible (%s) — gate de checksums omitido", exc)
        return
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info('schema_version')").fetchall()}
        if "checksum" not in cols:
            logger.info("almacén previo pre-migraciones (schema_version sin checksum) — gate omitido")
            return
        recorded = dict(con.execute("SELECT version, checksum FROM schema_version").fetchall())
    except duckdb.Error as exc:
        logger.warning("schema_version ilegible en el almacén previo (%s) — gate omitido", exc)
        return
    finally:
        con.close()
    by_version = {m.version: m for m in migs}
    for version, checksum in sorted(recorded.items()):
        mig = by_version.get(int(version))
        if mig is None:
            raise SystemExit(
                f"la migración {int(version):03d} está aplicada en el almacén vivo pero su archivo "
                f"desapareció de {MIGRATIONS_DIR} — el historial de migraciones es inmutable"
            )
        if mig.sha256 != checksum:
            raise SystemExit(
                f"checksum de la migración {int(version):03d} NO coincide con el aplicado en el almacén "
                f"vivo ({mig.sha256[:12]}… != {str(checksum)[:12]}…) — una migración aplicada jamás se "
                "edita; crea una migración nueva"
            )


# ─────────────────────────── deterministic timestamps (H4) ───────────────────────────


def _canon_date(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s).dt.strftime("%Y-%m-%d")
    return out.astype("string").fillna(_NULL_TOKEN)


def _canon_int(s: pd.Series) -> pd.Series:
    return s.astype("Int64").astype("string").fillna(_NULL_TOKEN)


def _canon_str(s: pd.Series) -> pd.Series:
    return s.astype("string").fillna(_NULL_TOKEN)


def _sha_rows(parts: list[pd.Series]) -> pd.Series:
    joined = parts[0].str.cat(list(parts[1:]), sep="|")
    return joined.map(lambda txt: hashlib.sha256(txt.encode("utf-8")).hexdigest())


def _fact_content_sha(df: pd.DataFrame) -> pd.Series:
    """Row-content hash of a priority fact (everything but the natural key)."""
    return _sha_rows(
        [
            _canon_str(df["status"]),
            _canon_date(df["priority_date"]),
            _canon_int(df["days_since_base"]),
            _canon_str(df["raw_value"]),
        ]
    )


def _dv_content_sha(df: pd.DataFrame) -> pd.Series:
    """Row-content hash of a DV rank fact (everything but the natural key)."""
    return _sha_rows(
        [
            _canon_str(df["status"]),
            _canon_int(df["rank_cutoff"]),
            _canon_str(df["raw_value"]),
            _canon_str(df["exceptions"]),
        ]
    )


def _derive_timestamps(
    new: pd.DataFrame, key_cols: list[str], prev: pd.DataFrame | None, ceiling: pd.Timestamp
) -> tuple[pd.Series, pd.Series]:
    """created_at/updated_at DERIVED FROM THE DATA (H4) — never wall-clock.

    created_at = the row's bulletin month (UTC midnight): the vintage of the
    snapshot that first published the cell. updated_at = created_at, UNLESS the
    previous live warehouse holds the same natural key with a DIFFERENT content
    hash — then it advances to the PANEL VINTAGE of this build (max bulletin
    month), i.e. the vintage of the data cut that introduced the change. Keys
    with an identical hash carry the previous updated_at forward, so a no-op
    rebuild reproduces both columns byte-identically. A from-scratch rebuild
    (no previous warehouse) re-derives everything from the data alone.
    """
    created = pd.to_datetime(new["bulletin_date"]).astype("datetime64[ns]").dt.tz_localize("UTC")
    if prev is None or prev.empty:
        return created, created.copy()
    # Positional by construction: both sides on a fresh RangeIndex so the merged
    # rows, the masks and the created series all align 1:1 with `new`.
    left = new[[*key_cols, "content_sha"]].reset_index(drop=True)
    left["bulletin_date"] = pd.to_datetime(left["bulletin_date"]).astype("datetime64[ns]")
    right = prev.rename(columns={"content_sha": "content_sha_prev", "updated_at": "updated_at_prev"}).reset_index(
        drop=True
    )
    right["bulletin_date"] = pd.to_datetime(right["bulletin_date"]).astype("datetime64[ns]")
    merged = left.merge(right, on=key_cols, how="left", validate="1:1")
    prev_upd = pd.to_datetime(merged["updated_at_prev"], utc=True).astype("datetime64[ns, UTC]")
    kept = merged["content_sha_prev"].notna() & (merged["content_sha_prev"] == merged["content_sha"])
    changed = merged["content_sha_prev"].notna() & ~kept
    updated = created.reset_index(drop=True)
    updated[kept] = prev_upd[kept]
    updated[changed] = ceiling
    updated = updated.where(updated >= created.reset_index(drop=True), created.reset_index(drop=True))
    updated.index = created.index  # re-align with the caller's frame
    return created, updated  # monotonía garantizada: CHECK created<=updated


@dataclass
class PreviousState:
    """Natural-key content hashes + updated_at of the previous live warehouse."""

    fact_priority: pd.DataFrame | None
    fact_dv: pd.DataFrame | None


def previous_state(db_path: str | Path) -> PreviousState | None:
    """Read the carry-forward state from the previous live database.

    Tolerant by design: no file, an unreadable file, a pre-timestamps schema or
    an incomplete build (etl_run != 1 row) mean NO history — updated_at is then
    re-derived purely from the data, which is exactly what a fresh clone does.
    """
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        logger.warning("almacén previo ilegible (%s) — updated_at se re-deriva de los datos", exc)
        return None
    try:
        con.execute("SET TimeZone='UTC'")
        cols = {r[1] for r in con.execute("PRAGMA table_info('fact_priority')").fetchall()}
        if "updated_at" not in cols:
            return None
        row = con.execute("SELECT count(*) FROM etl_run").fetchone()
        if not row or row[0] != 1:
            return None
        fp = con.execute(
            'SELECT a.slug AS country, c.block AS block, c.code AS category, t.code AS "table", '
            "d.bulletin_date AS bulletin_date, f.status, f.priority_date, f.days_since_base, "
            "f.raw_value, f.updated_at "
            "FROM fact_priority f "
            "JOIN dim_area     a ON a.area_id     = f.area_id "
            "JOIN dim_category c ON c.category_id = f.category_id "
            "JOIN dim_table    t ON t.table_id    = f.table_id "
            "JOIN dim_date     d ON d.date_id     = f.date_id"
        ).fetchdf()
        fp["content_sha"] = _fact_content_sha(fp)
        fp["updated_at"] = pd.to_datetime(fp["updated_at"], utc=True)
        fp = fp[["country", "block", "category", "table", "bulletin_date", "content_sha", "updated_at"]]
        dv = None
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        if "fact_dv_rank" in tables:
            dvdf = con.execute(
                "SELECT r.slug AS region, d.bulletin_date AS bulletin_date, f.status, f.rank_cutoff, "
                "f.raw_value, f.exceptions, f.updated_at "
                "FROM fact_dv_rank f "
                "JOIN dim_region r ON r.region_id = f.region_id "
                "JOIN dim_date   d ON d.date_id   = f.date_id"
            ).fetchdf()
            if len(dvdf):
                dvdf["content_sha"] = _dv_content_sha(dvdf)
                dvdf["updated_at"] = pd.to_datetime(dvdf["updated_at"], utc=True)
                dv = dvdf[["region", "bulletin_date", "content_sha", "updated_at"]]
    except duckdb.Error as exc:
        logger.warning("estado previo ilegible (%s) — updated_at se re-deriva de los datos", exc)
        return None
    finally:
        con.close()
    return PreviousState(fact_priority=fp, fact_dv=dv)


# ─────────────────────────── loaders ───────────────────────────


def _category_meta(code: str) -> tuple:
    """(parent_code, preference_level, is_subcategory, ina_basis) for a category
    code. Falls back to the leading digit if an unseen subcategory ever appears
    (the taxonomy test still pins the expected set)."""
    if code in CATEGORY_META:
        return CATEGORY_META[code]
    m = re.search(r"\d", code)
    return (None, int(m.group()) if m else 1, "_" in code, None)


def _load_dv(
    con: duckdb.DuckDBPyConnection,
    dv: pd.DataFrame,
    dim_date: pd.DataFrame,
    prev_dv: pd.DataFrame | None,
    ceiling: pd.Timestamp,
) -> None:
    """Load the Diversity-Visa region dimension and rank fact (region x month)."""
    d = dv.copy().reset_index(drop=True)
    d["bulletin_date"] = pd.to_datetime(d["visa_bulletin_date"])
    d["rank_cutoff"] = d["rank_cutoff"].astype("Int64")
    d["raw_value"] = d["raw_value"].astype("string")
    d["exceptions"] = d["exceptions"].astype("string") if "exceptions" in d.columns else pd.NA
    d["content_sha"] = _dv_content_sha(d)
    d["created_at"], d["updated_at"] = _derive_timestamps(d, ["region", "bulletin_date"], prev_dv, ceiling)
    d["etl_run_id"] = 1

    regions = sorted(d["region"].unique())
    dim_region = pd.DataFrame({"region_id": range(1, len(regions) + 1), "slug": regions})
    dim_region["name"] = dim_region["slug"].map(lambda s: REGION_NAMES.get(s, s))

    fact = d.merge(dim_region[["region_id", "slug"]], left_on="region", right_on="slug", validate="m:1").merge(
        dim_date[["date_id", "bulletin_date"]], on="bulletin_date", validate="m:1"
    )[
        [
            "region_id",
            "date_id",
            "status",
            "rank_cutoff",
            "raw_value",
            "exceptions",
            "etl_run_id",
            "created_at",
            "updated_at",
        ]
    ]

    con.register("v_dim_region", dim_region)
    con.register("v_fact_dv", fact)
    con.execute("INSERT INTO dim_region SELECT region_id, slug, name FROM v_dim_region")
    con.execute(
        "INSERT INTO fact_dv_rank (region_id, date_id, status, rank_cutoff, raw_value, exceptions, "
        "etl_run_id, created_at, updated_at) "
        "SELECT region_id, date_id, status, CAST(rank_cutoff AS INTEGER), raw_value, exceptions, "
        "etl_run_id, CAST(created_at AS TIMESTAMPTZ), CAST(updated_at AS TIMESTAMPTZ) FROM v_fact_dv"
    )


def _load_aliases(
    con: duckdb.DuckDBPyConnection, dim_category: pd.DataFrame, raw_dir: Path, allow_degraded: bool
) -> str | None:
    """Build dim_category_alias from the raw per-country CSVs: every published
    label -> canonical category, with the window of months it appeared.

    Returns a degradation reason (or None). H1: an empty lineage bridge used to
    be a WARNING with a green build — now it aborts unless --allow-degraded,
    and the concession is recorded in etl_run.build_status.
    """
    frames = []
    skipped = []
    for fp in sorted(raw_dir.glob("*_visa_backlog_timecourse.csv")):
        d = pd.read_csv(fp)
        if "raw_category" not in d.columns:
            skipped.append(fp.name)
            continue
        if "F_level" in d.columns:
            d["code"] = "F" + d["F_level"].astype(str)
            d["block"] = "family"
        else:
            d["code"] = d["EB_level"].astype(str)
            d["block"] = "employment"
        d["bulletin_date"] = pd.to_datetime(d["visa_bulletin_date"])
        d["raw_label"] = d["raw_category"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
        frames.append(d[["block", "code", "raw_label", "bulletin_date"]])
    # M4: a MIX is a scraper regression (some sources lost raw_category) and
    # always aborts — --allow-degraded is for missing inputs, not broken ones.
    if skipped and frames:
        raise SystemExit(
            f"raw_category ausente en {len(skipped)} de las fuentes ({skipped[:3]}…) — regresión del scraper"
        )
    if not frames:
        if not allow_degraded:
            raise SystemExit(
                f"ninguna fuente en {raw_dir} trae raw_category: dim_category_alias quedaría VACÍA. "
                "Aborta (usa --allow-degraded para construir degradado)."
            )
        logger.warning("ninguna fuente trae raw_category: dim_category_alias queda VACÍA (build degradado)")
        return "alias_lineage_missing"

    agg = (
        pd.concat(frames, ignore_index=True)
        .groupby(["block", "code", "raw_label"])
        .agg(
            valid_from=("bulletin_date", "min"),
            valid_to=("bulletin_date", "max"),
            n_months=("bulletin_date", "nunique"),
        )
        .reset_index()
        .merge(dim_category[["category_id", "block", "code"]], on=["block", "code"], validate="m:1")
        .sort_values(["category_id", "raw_label"])
        .reset_index(drop=True)
    )
    agg.insert(0, "alias_id", range(1, len(agg) + 1))
    # H4: envelope timestamps are pure functions of the data (first/last month observed).
    agg["created_at"] = pd.to_datetime(agg["valid_from"]).astype("datetime64[ns]").dt.tz_localize("UTC")
    agg["updated_at"] = pd.to_datetime(agg["valid_to"]).astype("datetime64[ns]").dt.tz_localize("UTC")

    con.register("v_dim_alias", agg)
    con.execute(
        "INSERT INTO dim_category_alias (alias_id, category_id, raw_label, valid_from, valid_to, n_months, "
        "created_at, updated_at) "
        "SELECT alias_id, category_id, raw_label, CAST(valid_from AS DATE), CAST(valid_to AS DATE), n_months, "
        "CAST(created_at AS TIMESTAMPTZ), CAST(updated_at AS TIMESTAMPTZ) FROM v_dim_alias"
    )
    return None


def _sources_frame(snapshots_dir: Path | None) -> pd.DataFrame | None:
    """source_artifact rows from the frozen bulletin HTML (H2): filename, S3
    archival URI, license, sha256 and the bulletin-month vintage parsed from the
    filename. Announcement pages without a mappable month stay out of scope."""
    if snapshots_dir is None or not Path(snapshots_dir).is_dir():
        return None
    from vp_data.visa_common import extract_datetime_from_link  # lazy: pulls requests/bs4

    rows = []
    for fp in sorted(Path(snapshots_dir).glob("*.html")):
        vintage = extract_datetime_from_link(fp.name)
        if vintage is None:
            logger.info("snapshot sin mes mapeable (fuera de procedencia): %s", fp.name)
            continue
        rows.append(
            {
                "filename": fp.name,
                "url": SNAPSHOTS_S3_PREFIX + fp.name,
                "license": SOURCE_LICENSE,
                "sha256": hashlib.sha256(fp.read_bytes()).hexdigest(),
                "vintage": pd.Timestamp(vintage),
            }
        )
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    frame.insert(0, "source_id", range(1, len(frame) + 1))
    stamp = frame["vintage"].astype("datetime64[ns]").dt.tz_localize("UTC")
    frame["created_at"] = stamp  # H4: derived from the artifact's own vintage
    frame["updated_at"] = stamp
    return frame


def _load_audit(
    con: duckdb.DuckDBPyConnection,
    schema_ver: int,
    run_info: dict | None,
    build_status: str,
    degradations: list[str],
) -> None:
    """Insert the single etl_run row LAST — it doubles as the completeness
    sentinel that vp_model.dataset._connect requires (M2). Identity fields come
    from run_info (CLI/env/derived); absent -> NULL, never fabricated.
    Wall-clock is legítimo here (bitácora) and only here."""
    counts = con.execute(
        "SELECT (SELECT count(*) FROM fact_priority), (SELECT count(*) FROM fact_dv_rank), "
        "(SELECT count(*) FROM fact_priority WHERE status = 'F'), "
        "(SELECT min(bulletin_date) FROM dim_date), (SELECT max(bulletin_date) FROM dim_date)"
    ).fetchone()
    assert counts is not None
    n_fp, n_dv, n_f, floor, ceiling = counts
    info = run_info or {}
    now = datetime.now(UTC)
    con.execute(
        "INSERT INTO etl_run (run_id, built_at_utc, schema_version, n_fact_priority, n_fact_dv, "
        "n_trainable_f, pct_trainable, panel_floor, panel_ceiling, pipeline_run_id, git_sha, git_dirty, "
        "panel_sha256, dvc_lock_sha256, env_lock_sha256, started_at, completed_at, build_status, degradations) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            now,
            schema_ver,
            n_fp,
            n_dv,
            n_f,
            n_f / n_fp if n_fp else 0.0,
            floor,
            ceiling,
            info.get("pipeline_run_id"),
            info.get("git_sha"),
            info.get("git_dirty"),
            info.get("panel_sha256"),
            info.get("dvc_lock_sha256"),
            info.get("env_lock_sha256"),
            info.get("started_at"),
            now,
            build_status,
            ", ".join(degradations) if degradations else None,
        ],
    )


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
    migs = list(migs) if migs is not None else migrations()
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


def content_fingerprint(db_path: str | Path) -> str:
    """sha256 over the warehouse's LOGICAL content (every base table, all rows,
    fully ordered), excluding only the bitácora wall-clock columns.

    This is the determinism contract of the .duckdb: the FILE is not
    byte-reproducible (bitácora + DuckDB internal storage order — why it lives
    outside DVC; see dvc.yaml), but two builds from the same inputs must return
    the SAME fingerprint. The Parquet export is byte-deterministic on its own
    (explicit ORDER BY) and is the DVC-tracked binary.
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute("SET TimeZone='UTC'")
        digest = hashlib.sha256()
        names = [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()]
        for tbl in names:
            skip = _VOLATILE_COLUMNS.get(tbl, set())
            cols = [r[1] for r in con.execute(f"PRAGMA table_info('{tbl}')").fetchall() if r[1] not in skip]
            col_list = ", ".join(f'"{c}"' for c in cols)
            digest.update(f"## {tbl}({col_list})\n".encode())
            for row in con.execute(f'SELECT {col_list} FROM "{tbl}" ORDER BY {col_list}').fetchall():
                digest.update(repr(row).encode())
                digest.update(b"\n")
        return digest.hexdigest()
    finally:
        con.close()


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


def _file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _git_identity() -> tuple[str | None, bool | None]:
    """(full sha, dirty) derived from the actual repo; (None, None) when git is
    unavailable — an honest NULL, never a fabricated identity."""
    root = Path(__file__).resolve().parents[1]
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=root, check=False)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=root, check=False)
    except OSError:
        return None, None
    full = sha.stdout.strip()
    if sha.returncode != 0 or len(full) != 40:
        return None, None
    return full, bool(status.stdout.strip()) if status.returncode == 0 else None


def _run_identity(args: argparse.Namespace, started_at: datetime) -> dict:
    """H2: build identity — CLI flag first, env second, derived from the real
    repo/files third, NULL last. Nothing here is ever invented by SQL."""
    git_sha = args.git_sha or os.environ.get("VP_GIT_SHA") or None
    dirty_raw = args.git_dirty or os.environ.get("VP_GIT_DIRTY", "").lower() or None
    git_dirty = {"true": True, "false": False}.get(dirty_raw) if dirty_raw else None
    if git_sha is None:
        git_sha, derived_dirty = _git_identity()
        if git_dirty is None:
            git_dirty = derived_dirty
    from vp_data.tracking import pipeline_run_id  # same id the ledger/manifest use (C3)

    env_lock = args.env_lock
    if env_lock is None:
        env_lock = Path(os.environ["VP_ENV_LOCK"]) if os.environ.get("VP_ENV_LOCK") else Path("locks/runtime.txt")
    return {
        "pipeline_run_id": args.pipeline_run_id or pipeline_run_id(),
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "panel_sha256": _file_sha256(PANEL_PATH),
        "dvc_lock_sha256": _file_sha256(args.dvc_lock),
        "env_lock_sha256": _file_sha256(env_lock),
        "started_at": started_at,
    }


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    started_at = datetime.now(UTC)
    df = pd.read_csv(PANEL_PATH, parse_dates=["bulletin_date", "priority_date"])
    dv = pd.read_csv(DV_RANK_PATH, parse_dates=["visa_bulletin_date"]) if DV_RANK_PATH.exists() else None
    migs = migrations()
    if DUCKDB_PATH.exists():
        # H1 fail-closed: an edited/deleted APPLIED migration aborts before any build.
        verify_migration_history(DUCKDB_PATH, migs)
    # H4: carry updated_at forward from the previous live warehouse (no-op
    # rebuilds keep both timestamp columns byte-identical).
    prev = previous_state(DUCKDB_PATH)
    run_info = _run_identity(args, started_at)
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
