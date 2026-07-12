"""Dependency-light constants shared by the data-product scripts
(build_panel, audits, visualizers).

Scraping-specific constants (URLs, retry, scraper country order) live in
``visa_common.py``; this module holds only the data-product configuration and
has no heavy imports, so build_panel/audits don't pull in requests/bs4.
"""

from pathlib import Path

DATA_DIR = Path("data")
# cookiecutter-data-science layout: raw = scraped source CSVs (one per country),
# processed = the consolidated long panel (derived). Keep them apart so a
# consumer can tell source from derivative without reading the README.
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
PANEL_PATH = PROCESSED_DIR / "visa_panel_long.csv"
BULLETINS_JSON_PATH = PROCESSED_DIR / "bulletins.json"  # feed del último boletín (web)
# Diversity Visa regional rank cut-offs (separate dataset: rank, not date).
DV_RANK_PATH = RAW_DIR / "dv_visa_rank_timecourse.csv"
# Frozen bulletin HTML (gitignored; the S3 bucket is the master copy — see the
# freeze workflow). Single source: freeze_snapshots writes here and
# build_database reads it to register source_artifact provenance (H2).
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
# Archival URI prefix for a snapshot (its stable, resolvable source of truth;
# the original travel.state.gov href is not persisted at freeze time).
SNAPSHOTS_S3_PREFIX = "s3://visapredictai-raw-snapshots/raw-html/"
# Normalized star-schema database + typed columnar export, both regenerated from
# PANEL_PATH by build_database.py (gitignored; the CSV is the versioned artifact).
DUCKDB_PATH = PROCESSED_DIR / "visapredict.duckdb"
PARQUET_PATH = PROCESSED_DIR / "visa_panel_long.parquet"

# Dependent-variable epoch (string; build_panel wraps it in pd.Timestamp).
# Chosen before the earliest observed priority date (1979-11, Philippines F4).
BASE_EPOCH = "1975-01-01"
BASE_EPOCH_YEAR = int(BASE_EPOCH.split("-")[0])  # derivado, no tipeado (t0 es 1-ene)

# Days->years conversion. Single source (audit r4): lived in vp_model.config, so
# the DATA layer (build_panel/visa_common/mega_audit/build_eda_facts) re-typed
# `365.25` inline — the exact "cero 365.25 a mano" the project claims. Now here,
# in the base layer everyone can import; vp_model.config re-exports it.
DAYS_PER_YEAR = 365.25
BIG_JUMP_YEARS = 8  # umbral "salto grande" del ledger de limpieza + mega_audit d9


def days_to_year(days):  # noqa: ANN001, ANN201 — escalar/Series/ndarray por igual
    """días desde BASE_EPOCH -> año calendario fraccional (ejes de figuras)."""
    return BASE_EPOCH_YEAR + days / DAYS_PER_YEAR


# Raw scraper slug -> canonical panel label.
CANONICAL_COUNTRY = {
    "mexico": "mexico",
    "india": "india",
    "china": "china",
    "philippines": "philippines",
    "row": "all_chargeability",  # "All Chargeability Areas Except Those Listed"
}

# visa-bulletin table_type -> short code.
TABLE_MAP = {"final_action": "FAD", "dates_for_filing": "DFF"}

# Months absent from the official site (404 + Wayback-only). The 5 dead months
# (2009-03/09/10/11, 2012-10) were recovered by hand from the archive into
# data/snapshots/ and are now parsed (I1) — coverage is 296/296, nothing is
# whitelisted anymore. Kept as an (empty) hook for any future genuinely-dead month.
DEAD_MONTHS: list[str] = []

# UACJ / MIAAD institutional palette.
UACJ_AZUL = "#003CA6"
UACJ_AMARILLO = "#FFD600"
UACJ_GRIS = "#555559"
UACJ_NEGRO = "#231F20"
