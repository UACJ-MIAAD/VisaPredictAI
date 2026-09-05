"""Scrape the Employment-Based tables (FAD + DFF) from every monthly U.S. Visa
Bulletin and write one CSV per country to ``data/raw/``.

Run from the repo root:
    ante/bin/python -m pipeline.scrape_visa_bulletins
"""

import logging
import re

import pandas as pd
from tqdm import tqdm

from vp_data.categories import classify_eb
from vp_data.config import RAW_DIR
from vp_data.extract import extract_country_data as shared_extract
from vp_data.visa_common import (
    SCRAPER_COUNTRIES,
    SITE_ROOT,
    check_country_coverage,
    extract_datetime_from_link,
    extract_month_links,
    get_soup,
    parse_tables,
    report_failures,
)

logger = logging.getLogger(__name__)


def is_employment_section(rows) -> bool:
    """A table is the employment section if a row mentions 'employment-based',
    tolerating spacing drift ('employment- based', 'employment based')."""
    return any(re.search(r"employment[\s-]*based", row.get_text(strip=True).lower()) for row in rows)


def extract_tables(link: str) -> list[pd.DataFrame]:
    return parse_tables(get_soup(SITE_ROOT + link), extract_datetime_from_link(link), is_employment_section)


def classify_eb_category(raw) -> None | str:
    """Compatibilidad: la taxonomía vive en `vp_data.categories` (C1).

    Se conserva el nombre porque es la entrada pública que usa el resto del scraper;
    las reglas —y su orden semántico— ya no se duplican aquí.
    """
    return classify_eb(raw)


def extract_country_data(country: str, all_data: list[pd.DataFrame]) -> pd.DataFrame:
    """Compatibilidad: el algoritmo vive en `vp_data.extract` (C2), parametrizado por la
    columna de nivel y el clasificador. Aquí solo queda la elección de esos dos."""
    return shared_extract(country, all_data, level_col="EB_level", classifier=classify_eb)


def write_csvs(all_data: list[pd.DataFrame]) -> None:
    """Write one employment CSV per country from the parsed tables. Shared by
    this script's main() and the single-fetch scrape_all.py driver."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # K2: months carried by ANY parsed table — the yardstick each country is
    # audited against (a vanished country column is invisible to the row gates).
    all_months = {m for df in all_data for m in pd.to_datetime(df["visa_bulletin_date"]).dropna()}
    for country in tqdm(SCRAPER_COUNTRIES, desc="Extracting data for each country and computing backlogs"):
        country_df = extract_country_data(country, all_data)
        check_country_coverage(country, country_df, all_months, logger)
        # Deterministic order (newest first, then table then category): a fully
        # specifying key, so a transient dropped month cannot cascade-reorder the
        # rest via an unstable sort.
        country_df = country_df.sort_values(
            by=["visa_bulletin_date", "table_type", "EB_level"], ascending=[False, True, True]
        )
        country_df.to_csv(RAW_DIR / f"{country}_visa_backlog_timecourse.csv", index=False)


def main():
    month_links = extract_month_links()
    all_data = []
    failed = []
    for link in tqdm(month_links, desc="Extracting all employment-based visa bulletin tables"):
        try:
            all_data.extend(extract_tables(link))
        except Exception as exc:
            failed.append((link, str(exc)[:60]))
    report_failures(failed, logger)
    write_csvs(all_data)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
