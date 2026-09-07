"""Cargadores y canonización de filas del almacén.

Lo que convierte el panel plano y sus fuentes auxiliares en las filas que entran a las
dimensiones y a los hechos: canonización de fechas, enteros y cadenas; el sha256 de
contenido por fila; las marcas `created_at`/`updated_at` DERIVADAS DEL DATO (H4, nunca
reloj de pared); el estado de la base anterior, y la carga de la lotería de diversidad,
los alias de categoría y el registro de artefactos fuente.

Aquí no vive el DDL ni la bitácora: solo el dato y su forma canónica.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from vp_data.categories import CATEGORY_META  # C1: autoridad única de la taxonomía
from vp_data.config import SNAPSHOTS_S3_PREFIX

logger = logging.getLogger(__name__)

# C1: la taxonomía sigue viniendo de `vp_data.categories`; aquí solo se reexporta para
# que quien consumía `build_database.CATEGORY_META` no tenga que cambiar de sitio.
__all__ = ["CATEGORY_META", "SOURCE_LICENSE", "AREA_NAMES", "TABLE_NAMES", "STATUS_META", "REGION_NAMES"]

# Token NULL canónico para el hash de contenido por fila (no puede salir en una celda real).
_NULL_TOKEN = "\x00"
# El Visa Bulletin es obra del gobierno federal de EE. UU. (sin derechos de autor).
SOURCE_LICENSE = "U.S. Government work - public domain (17 U.S.C. 105)"

# Nombres visibles de las filas de dimensión (el panel guarda solo el slug o el código).
AREA_NAMES = {
    "mexico": "México",
    "india": "India",
    "china": "China (mainland born)",
    "philippines": "Philippines",
    "all_chargeability": "All Chargeability Areas Except Those Listed",
}
TABLE_NAMES = {"FAD": "Final Action Dates", "DFF": "Dates for Filing"}
# Régimen C/F/U/UNK, promovido a dim_status. Solo 'F' es objetivo de modelado.
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
