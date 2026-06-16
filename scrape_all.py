"""Single-fetch driver for all three Visa Bulletin scrapers.

Fetches each monthly bulletin page ONCE and parses all three sections
(employment, family, diversity visa) from the same soup — instead of the three
standalone scrapers each re-downloading every ~290 pages (3x the HTTP traffic,
~12 min). Every per-block extractor is reused verbatim; only the fetch loop is
shared, so behavior is identical to running the three scripts in sequence.

A failed month drops from all three blocks (counted once, then the shared
MAX_FETCH_FAILURES gate applies) — the same whole-month-drop the standalone
scrapers use.

    ante/bin/python scrape_all.py
"""

import logging

from tqdm import tqdm

import scrape_dv_visa_bulletins as dv
import scrape_family_visa_bulletins as fam
import scrape_visa_bulletins as emp
from visa_common import (
    SITE_ROOT,
    extract_datetime_from_link,
    extract_month_links,
    get_soup,
    parse_tables,
    report_failures,
)

logger = logging.getLogger(__name__)


def main() -> None:
    month_links = extract_month_links()

    emp_tables = []
    fam_tables = []
    dv_frames = []
    failed = []
    for link in tqdm(month_links, desc="Fetching each bulletin once (employment + family + DV)"):
        try:
            soup = get_soup(SITE_ROOT + link)
            ym = extract_datetime_from_link(link)
            emp_tables.extend(parse_tables(soup, ym, emp.is_employment_section))
            fam_tables.extend(parse_tables(soup, ym, fam.is_family_section))
            rows = dv.extract_month_rows(soup, ym)
            if not rows.empty:
                dv_frames.append(rows)
        except Exception as exc:
            failed.append((link, str(exc)[:60]))

    report_failures(failed, logger)
    emp.write_csvs(emp_tables)
    fam.write_csvs(fam_tables)
    dv.finalize(dv_frames)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
