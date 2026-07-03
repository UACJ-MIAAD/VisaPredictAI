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
# Diversity Visa regional rank cut-offs (separate dataset: rank, not date).
DV_RANK_PATH = RAW_DIR / "dv_visa_rank_timecourse.csv"
# Normalized star-schema database + typed columnar export, both regenerated from
# PANEL_PATH by build_database.py (gitignored; the CSV is the versioned artifact).
DUCKDB_PATH = PROCESSED_DIR / "visapredict.duckdb"
PARQUET_PATH = PROCESSED_DIR / "visa_panel_long.parquet"

# Dependent-variable epoch (string; build_panel wraps it in pd.Timestamp).
# Chosen before the earliest observed priority date (1979-11, Philippines F4).
BASE_EPOCH = "1975-01-01"

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
