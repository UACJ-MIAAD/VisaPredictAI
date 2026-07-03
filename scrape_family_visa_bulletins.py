"""Scrape the Family-Sponsored tables (FAD + DFF) from every monthly U.S. Visa
Bulletin and write one CSV per country to ``data/raw/``.

Run from the repo root:
    ante/bin/python scrape_family_visa_bulletins.py
"""

import logging

import pandas as pd
from tqdm import tqdm

from config import RAW_DIR
from visa_common import (
    SCRAPER_COUNTRIES,
    SITE_ROOT,
    annotate_dates,
    extract_datetime_from_link,
    extract_month_links,
    get_soup,
    norm_label,
    parse_tables,
    report_failures,
)

logger = logging.getLogger(__name__)


def is_family_section(rows) -> bool:
    """A table is the family section if a row contains the 'family' substring
    (the header is 'family' / 'family- sponsored', sometimes concatenated as
    'familyall chargeability...' in 2007-2008). Employment and diversity-visa
    tables never contain it."""
    return any("family" in row.get_text(strip=True).lower() for row in rows)


def extract_tables(link: str) -> list[pd.DataFrame]:
    return parse_tables(get_soup(SITE_ROOT + link), extract_datetime_from_link(link), is_family_section)


def classify_family_category(raw) -> None | str:
    """Map a raw 'Family-Sponsored' row label to a canonical level code,
    absorbing label drift ('1st'->F1 in 2006-2011, 'F1' from 2011 on, and the
    '*' footnote variants). Returns None for non-category rows (e.g. the
    'family' spanning header). Codes match the legacy values: 1, 2A, 2B, 3, 4.
    """
    s = norm_label(raw)
    # J3: same footnote tolerance as classify_eb_category (H3). The hardcoded
    # '2a*'/'2b*' variants proved the source DOES footnote family rows; the
    # other levels ('F1*', '4th*') simply hadn't happened yet and would have
    # dropped the month for that series in silence.
    s = s.rstrip("*† ")
    if s in ("1st", "f1"):
        return "1"
    if s in ("2a", "2nd-a", "2nda", "f2a"):
        return "2A"
    if s in ("2b", "2nd-b", "2ndb", "f2b"):
        return "2B"
    if s in ("3rd", "f3"):
        return "3"
    if s in ("4th", "4rd", "f4"):  # '4rd' = typo de la fuente (2003-03) -> recupera 3 celdas F4
        return "4"
    return None


def extract_country_data(country: str, all_data: list[pd.DataFrame]) -> pd.DataFrame:
    # 'row' (Rest of World) lives in the "all chargeability areas except
    # those listed" column; match 'except those listed', which is stable
    # even when older bulletins split 'chargeability' as 'charge ability'.
    search_country = "except those listed" if country == "row" else country

    country_data = []
    for df in all_data:
        norm = {col: norm_label(col) for col in df.columns}

        # The family-category column is always column 0 (header is
        # 'family- sponsored', 'family', or '' across the years).
        cat_col = df.columns[0]
        country_col = next((c for c in df.columns if search_country in norm[c]), None)
        if country_col is None or country_col == cat_col:
            continue

        try:
            sub = df[[cat_col, country_col, "visa_bulletin_date", "table_type"]].copy()
        except KeyError, ValueError:
            # ValueError: a duplicate normalized header makes df[country_col] a
            # frame, so the column-rename below would mismatch. Skip that table.
            continue
        sub.columns = ["F_level", "priority_date", "visa_bulletin_date", "table_type"]
        country_data.append(sub)

    if not country_data:
        return pd.DataFrame(
            columns=["F_level", "priority_date", "visa_bulletin_date", "visa_wait_time", "table_type", "raw_category"]
        )

    country_df = pd.concat(country_data, axis=0, ignore_index=True)
    country_df = country_df[country_df["visa_bulletin_date"].notna()]

    # raw_value / status / parse priority_date / visa_wait_time (H1 annotation).
    country_df = annotate_dates(country_df, "priority_date")

    # Preserve the raw published label before normalizing it, so
    # dim_category_alias can document 20 years of label drift (lineage).
    # Per-line rstrip: the 2009 archive bulletins publish multi-line labels with
    # trailing spaces per line; leaving them writes line-trailing whitespace into
    # the quoted CSV field, which the repo's whitespace hook then rewrites (churn).
    country_df["raw_category"] = (
        country_df["F_level"].astype(str).str.strip().str.replace(r"[ \t]+\n", "\n", regex=True)
    )

    # Map the raw 'Family-Sponsored' label to a canonical level code
    # (1, 2A, 2B, 3, 4); drop rows that are not a family category.
    country_df["F_level"] = country_df["F_level"].apply(classify_family_category)
    country_df = country_df[country_df["F_level"].notna()]

    # Keep a unique (level, month, table) key (guards against any label
    # transition putting the same category twice in one bulletin).
    country_df = country_df.drop_duplicates(subset=["F_level", "visa_bulletin_date", "table_type"], keep="first")

    return country_df


def write_csvs(all_data: list[pd.DataFrame]) -> None:
    """Write one family CSV per country from the parsed tables. Shared by this
    script's main() and the single-fetch scrape_all.py driver."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for country in tqdm(SCRAPER_COUNTRIES, desc="Extracting data for each country and computing backlogs"):
        country_df = extract_country_data(country, all_data)
        # Deterministic order (newest first, then table then category): a fully
        # specifying key, so a transient dropped month cannot cascade-reorder the
        # rest via an unstable sort.
        country_df = country_df.sort_values(
            by=["visa_bulletin_date", "table_type", "F_level"], ascending=[False, True, True]
        )
        country_df.to_csv(RAW_DIR / f"{country}_family_visa_backlog_timecourse.csv", index=False)


def main():
    month_links = extract_month_links()
    all_data = []
    failed = []
    for link in tqdm(month_links, desc="Extracting all family-sponsored visa bulletin tables"):
        try:
            all_data.extend(extract_tables(link))
        except Exception as exc:
            failed.append((link, str(exc)[:60]))
    report_failures(failed, logger)
    write_csvs(all_data)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
