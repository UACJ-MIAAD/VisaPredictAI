"""Dependency-light constants shared by the data-product scripts
(build_panel, audits, visualizers).

Scraping-specific constants (URLs, retry, scraper country order) live in
``visa_common.py``; this module holds only the data-product configuration and
has no heavy imports, so build_panel/audits don't pull in requests/bs4.
"""

from pathlib import Path

DATA_DIR = Path("data")
PANEL_PATH = DATA_DIR / "visa_panel_long.csv"

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

# Months absent from the official site (404 + Wayback-only); see deep search.
DEAD_MONTHS = ["2009-03", "2009-09", "2009-10", "2009-11", "2012-10"]

# UACJ / MIAAD institutional palette.
UACJ_AZUL = "#003CA6"
UACJ_AMARILLO = "#FFD600"
UACJ_GRIS = "#555559"
UACJ_NEGRO = "#231F20"
