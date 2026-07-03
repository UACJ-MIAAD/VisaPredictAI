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

from bs4 import BeautifulSoup
from tqdm import tqdm

import scrape_dv_visa_bulletins as dv
import scrape_family_visa_bulletins as fam
import scrape_visa_bulletins as emp
from freeze_snapshots import SNAP_DIR
from visa_common import extract_datetime_from_link, parse_tables, report_failures

logger = logging.getLogger(__name__)


def main() -> None:
    # Parse the frozen HTML snapshots OFFLINE -- the bulletins are fixed, so the
    # only live fetch is freeze_snapshots.py grabbing a newly published month.
    # Same parser + same HTML the live scrape used, so the CSVs are identical.
    snapshots = sorted(SNAP_DIR.glob("*.html"))
    if not snapshots:
        raise SystemExit(f"No snapshots in {SNAP_DIR}/ -- run freeze_snapshots.py (or `aws s3 sync` them down) first")

    emp_tables = []
    fam_tables = []
    dv_frames = []
    failed = []  # fallos del PANEL objetivo (employment+family) -- esto sí es "mes perdido"
    dv_failed = []  # fallos SOLO del parser DV (dataset no-predictivo) -- NO contamina el panel
    for path in tqdm(snapshots, desc="Parsing frozen bulletins offline (employment + family + DV)"):
        try:
            # J6: errors="replace" — mismo criterio que el gate de freeze. Una página
            # cp1252 pasaba el freeze (que valida con replace) y reventaba AQUÍ con
            # decode estricto, convirtiendo un byte raro en un mes perdido.
            soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
            ym = extract_datetime_from_link(path.name)
        except Exception as exc:
            failed.append((path.name, f"soup/ym: {str(exc)[:50]}"))
            continue
        # I1: a snapshot whose filename doesn't map to a month used to flow on with
        # ym=None and get dropped row-by-row downstream (notna filter) — zero rows,
        # zero warnings. That silence hid 5 real, complete bulletins for months.
        # Skip it HERE and say so; if it's a real bulletin, extend the regex.
        if ym is None:
            logger.warning("snapshot sin mes mapeable (se OMITE del panel): %s", path.name)
            continue
        # Sección objetivo (panel): su fallo SÍ es un mes perdido.
        try:
            e_parsed = parse_tables(soup, ym, emp.is_employment_section)
            f_parsed = parse_tables(soup, ym, fam.is_family_section)
        except Exception as exc:
            failed.append((path.name, str(exc)[:60]))
        else:  # all-or-nothing REAL: si familia truena, empleo no queda medio-poblado (H1)
            emp_tables.extend(e_parsed)
            fam_tables.extend(f_parsed)
        # Diversity Visa: AISLADO -- un fallo aquí NO marca el mes como perdido ni cuenta
        # contra el gate del panel (antes un IndexError de DV tiraba el mes entero).
        try:
            rows = dv.extract_month_rows(soup, ym)
            if not rows.empty:
                dv_frames.append(rows)
        except Exception as exc:
            dv_failed.append((path.name, str(exc)[:60]))

    report_failures(failed, logger)
    if dv_failed:
        logger.warning(
            "DV parser falló en %d meses (dataset no-predictivo; panel objetivo intacto): %s",
            len(dv_failed),
            [f[0] for f in dv_failed],
        )
    emp.write_csvs(emp_tables)
    fam.write_csvs(fam_tables)
    dv.finalize(dv_frames)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
