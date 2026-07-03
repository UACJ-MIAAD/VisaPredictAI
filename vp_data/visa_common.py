"""Shared helpers for the Visa Bulletin scrapers.

Centralizes the fetch / link-discovery / date / status functions that were
byte-for-byte (modulo comments) duplicated across ``scrape_visa_bulletins.py``
and ``scrape_family_visa_bulletins.py``. Each scraper now imports from here and
keeps only what genuinely differs (section detection, category mapping, output
columns).
"""

import logging
import re
import time
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---- configuration (single source of truth) ----------------------------
BASE_URL = "https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html"
SITE_ROOT = "https://travel.state.gov/"
# Countries scraped, in the order the per-country CSVs are written. 'row' =
# "All Chargeability Areas Except Those Listed".
SCRAPER_COUNTRIES = ["india", "china", "mexico", "philippines", "row"]
DATE_FMT = "%d%b%y"
# A published cell may carry a footnote marker ("15JUL05*", "01MAY16 1"); the exact strptime
# would drop it to UNK. As a FALLBACK (only after the exact parse fails) we extract the first
# DDMMMYY token so a footnoted date is still recognized. No effect on current data (0 footnoted
# cells today); robustness against format drift (audit finding).
_DATE_TOKEN = re.compile(r"\d{1,2}[A-Z]{3}\d{2}(?!\d)")  # (?!\d): un futuro 01JAN2015 NO es 01JAN20
logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 6  # a couple of months (e.g. 2007-12) hit an intermittent redirect loop
# A handful of months can fail transiently (a redirect loop, a 5xx); the run
# proceeds and the failure-reporter logs them. But if MORE than this many fail,
# something is structurally wrong with the source (site redesign, outage) and
# the scraper aborts WITHOUT writing, so a degraded panel is never published.
MAX_FETCH_FAILURES = 10

MONTH_MAP = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


# ---- link discovery -----------------------------------------------------
def extract_datetime_from_link(link: str) -> None | datetime:
    """Parse a bulletin filename into a datetime (day=1).

    Accepts both naming variants the source has actually used:
    'visa-bulletin-for-<month>-<year>.html' (canonical since ~2003) and
    'visa-bulletin-<month>-<year>.html' (the 5 archive months recovered by hand:
    2009-03/09/10/11 and 2012-10 — I1: the 'for-'-only regex silently ignored
    those real, complete bulletins sitting in data/snapshots/ for months).
    """
    match = re.search(r"visa-bulletin(?:-for)?-(\w+)-(\d{4})\.html$", link)
    if not match:
        return None
    month_str, year = match.groups()
    month = MONTH_MAP.get(month_str.lower())
    if not month:
        return None
    return datetime(year=int(year), month=month, day=1)


def get_soup(url: str, retries: int = MAX_RETRIES) -> BeautifulSoup:
    """GET with retry+backoff and a timeout, raising on non-200.

    A bare ``requests.get`` plus ``except: pass`` in the caller was silently
    dropping a whole month on any transient HTTP blip; the retry prevents that.
    """
    last: Exception = RuntimeError(f"no fetch attempt for {url}")
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.HTTPError as exc:
            # A 4xx is permanent (e.g. a 404 for a month that never existed):
            # retrying only burns the backoff. Retry 5xx (transient server side).
            if exc.response is not None and 400 <= exc.response.status_code < 500:
                raise
            last = exc
            time.sleep(2 * (attempt + 1))
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def extract_month_links() -> list[str]:
    """Return every monthly-bulletin href listed in the page accordion."""
    soup = get_soup(BASE_URL)
    month_links = []
    for section in soup.find_all("div", class_="accordion parbase section"):
        link_container = section.find("div", class_="tsg-rwd-accordion-copy")
        if link_container:
            for link in link_container.find_all("a", href=True):
                month_links.append(str(link["href"]))
    return month_links


def report_failures(failed: list[tuple[str, str]], logger) -> None:
    """Shared post-loop failure accounting for every scraper: log each month
    that failed after retries, and abort (without writing) if MORE than
    MAX_FETCH_FAILURES did — that many failures means the source changed
    structurally, not a transient blip, so a degraded panel must not ship."""
    if not failed:
        return
    logger.warning("%d boletines fallaron tras reintentos (meses perdidos):", len(failed))
    for link, err in failed:
        logger.warning("   %s  %s", link.split("/")[-1], err)
    if len(failed) > MAX_FETCH_FAILURES:
        raise SystemExit(
            f"{len(failed)} boletines fallaron (> {MAX_FETCH_FAILURES}): probable "
            f"problema de la fuente, no un blip transitorio. Se aborta sin escribir "
            f"para no publicar un panel degradado."
        )


def check_country_coverage(country: str, country_df: pd.DataFrame, all_months: set, logger) -> None:
    """K2: per-(country, month) accounting the gates were blind to.

    A renamed country header ('CHINA-mainland born' → something without the
    substring) or a duplicated header makes extract_country_data skip that
    column with a mute ``continue`` — the country vanishes from that month on
    with every downstream gate green (they check the month-union, not who
    carries it). Old bulletins legitimately lack some columns (China has no own
    EB column before 2005-04), so historical gaps are a one-line WARNING; but
    the NEWEST parsed month missing a country is a live parser regression and
    aborts before writing.
    """
    got = set(pd.to_datetime(country_df["visa_bulletin_date"]).dropna())
    missing = all_months - got
    if not all_months:
        return
    newest = max(all_months)
    if newest not in got:
        raise SystemExit(
            f"{country}: sin datos en el mes más reciente ({newest:%Y-%m}) — "
            "¿cambió el header de país en la fuente? Se aborta sin escribir."
        )
    if missing:
        logger.warning(
            "%s: %d/%d meses sin columna propia (viejos formatos: esperado; vigilar si crece)",
            country,
            len(missing),
            len(all_months),
        )


# ---- cell parsing / annotation -----------------------------------------
def string_to_datetime(date_str: str, bulletin_date: datetime) -> None | datetime:
    """Convert a published cell to a date. 'C' -> bulletin date (legacy
    behavior kept for the wait-time column); 'U'/empty/unparseable -> None.

    Whitespace/case-normalized to match ``classify_status`` (so ' C ' / 'c' don't parse
    inconsistently). Guards the ``%y`` century pivot: a priority date is never much later
    than its bulletin, so a 2-digit year that lands in the future (>bulletin+1) is a wrong
    20xx pivot of a 19xx date -> corrected by -100 years (latent before 2027; harmless today).
    """
    if pd.isna(date_str):
        return None
    s = str(date_str).strip()
    su = s.upper().rstrip("*† ")  # J4: a footnoted 'C*'/'U*' is still Current/Unavailable
    if su == "C":
        return bulletin_date
    if su in ("U", ""):
        return None
    try:
        d = datetime.strptime(s, DATE_FMT)
    except ValueError:
        m = _DATE_TOKEN.search(su)  # fallback: a footnoted date like "15JUL05*"
        if not m:
            return None
        try:
            d = datetime.strptime(m.group(), DATE_FMT)
        except ValueError:
            return None
    # %y century pivot: a FAD/DFF cutoff is never AFTER its own bulletin (you process dates
    # from years ago), so any parsed date later than the bulletin is a wrong 20xx pivot of a
    # 19xx date -> correct by -100 years. Pivot on the full date (not year>bulletin+1, which
    # let a date one year in the future slip through; latent until 2027, fixed by the audit).
    if (d.year, d.month) > (bulletin_date.year, bulletin_date.month):
        # comparar por MES: el boletín se fecha al día 1, y una celda legítima del propio
        # mes del boletín (día >= 2) NO debe corregirse -100 años (hallazgo H1)
        logger.info("pivote de siglo: %s > boletín %s -> -100 años", s, f"{bulletin_date:%Y-%m}")
        d = d.replace(year=d.year - 100)
    return d


def classify_status(date_str) -> str:
    """Annotate the published cell with its visa-bulletin regime:

    * ``F``   a specific final-action / dates-for-filing date is published
    * ``C``   Current -- no backlog this month
    * ``U``   Unavailable -- no numbers available this month
    * ``UNK`` empty or unparseable cell

    Preserves the regime that is otherwise lost when 'C' is flattened to the
    bulletin date and 'U' to NaN. Only rows with status 'F' are a prediction
    target (v5.1). ``UNK`` (not ``NA``) is used on purpose: pandas coerces the
    string ``"NA"`` to NaN on read, which would erase the annotation.
    """
    if pd.isna(date_str):
        return "UNK"
    s = str(date_str).strip().upper()
    if s == "":
        return "UNK"
    # J4: the same footnote paranoia already applied to dates (_DATE_TOKEN) and
    # category labels (rstrip in both classifiers) — status letters are the cells
    # MOST likely to get an asterisk in a retrogression note, and 'C*' fell to UNK.
    if s.rstrip("*† ") == "C":
        return "C"
    if s.rstrip("*† ") == "U":
        return "U"
    try:
        datetime.strptime(str(date_str).strip(), DATE_FMT)
        return "F"
    except ValueError:
        # J1: the token regex alone accepted impossible dates ("31JUN26", "00MAY16",
        # "15XYZ05") as F while string_to_datetime returned None for them — one
        # source typo then killed the whole cron via the panel's F-with-NaT
        # fail-fast. Validate the token with strptime so both functions agree by
        # construction: parseable footnoted date -> F, garbage -> UNK.
        m = _DATE_TOKEN.search(s)
        if not m:
            return "UNK"
        try:
            datetime.strptime(m.group(), DATE_FMT)
            return "F"  # footnoted date ("15JUL05*") still F
        except ValueError:
            return "UNK"


def norm_label(s) -> str:
    r"""Collapse whitespace noise (\n, \xa0, runs of spaces) and lowercase."""
    if pd.isna(s):
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip().lower()


# ---- table parsing (pure: soup -> dataframes; testable offline) ---------
# J2: FAD/DFF used to be decided purely by table ORDINAL (1st match = FAD, 2nd =
# DFF), so any extra <table> the source slips in before the real ones silently
# shifted every label (FAD data published as DFF — corruption no gate catches).
# Modern bulletins announce each table in the prose right before it; label by
# that marker and keep the ordinal only as fallback for pre-2015 layouts.
_TABLE_TYPE_MARKER = re.compile(r"(?i)final action|dates for filing")


def parse_tables(soup: BeautifulSoup, year_month, section_matcher, label_by_marker: bool = True) -> list[pd.DataFrame]:
    """Parse the preference tables a ``section_matcher(rows) -> bool`` selects.

    Decoupled from fetching so it can be unit-tested with saved HTML fixtures
    (no network). Each matching table is tagged ``final_action`` or
    ``dates_for_filing`` by the nearest preceding FAD/DFF heading (J2); when no
    heading exists (pre-Oct-2015 layouts have a single unannounced FAD table)
    the ordinal fallback applies. ``label_by_marker=False`` keeps the pure
    ordinal labeling: the DV section's two tables are current-month ranks vs.
    advance notification (not FAD/DFF), and the nearest preceding heading there
    belongs to the FAMILY section, which would mislabel both. ``section_matcher``
    is what makes this open to new sections (employment, family, …) without
    editing the parser.
    """
    dfs = []
    seen_types: set[str] = set()
    table_count = 0
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not section_matcher(rows):
            continue
        table_count += 1
        marker = table.find_previous(string=_TABLE_TYPE_MARKER) if label_by_marker else None
        if marker is not None:
            table_type = "dates_for_filing" if "filing" in str(marker).lower() else "final_action"
        else:
            table_type = "final_action" if table_count == 1 else "dates_for_filing"
            if label_by_marker and table_count >= 2:
                logger.warning(
                    "tabla %d de %s sin heading FAD/DFF: se etiqueta por ordinal (%s)",
                    table_count,
                    year_month,
                    table_type,
                )

        table_data = []
        for row in rows:
            # J7: document order — concatenating th_cols + td_cols distorted the
            # column order if a row ever mixed <td> before <th> (country shift).
            cols = [ele.text.strip() for ele in row.find_all(["th", "td"])]
            table_data.append(cols)

        # A single-cell first row is a spanning header; drop it.
        if len(table_data[0]) == 1:
            columns = table_data[1]
            table_body = table_data[2:]
        else:
            columns = table_data[0]
            table_body = table_data[1:]

        df = pd.DataFrame(table_body, columns=columns)
        df["visa_bulletin_date"] = year_month
        df["table_type"] = table_type
        df.columns = df.columns.str.replace("\n", "").str.replace("- ", "-")
        df.columns = df.columns.str.lower()
        dfs.append(df)

        # J2: stop once both table types hold DATA (a stray note-table — which
        # parses to 0 rows — no longer evicts the real DFF, which the hard
        # `>= 2` break used to do); the cap of 4 scanned matches guards against
        # pathological layouts.
        if not df.empty:
            seen_types.add(table_type)
        if seen_types == {"final_action", "dates_for_filing"} or table_count >= 4:
            break
    return dfs


def annotate_dates(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """Annotate a per-country frame whose ``value_col`` holds the raw published
    cells. Adds ``raw_value`` and ``status``, parses ``value_col`` into a date
    in place, and adds ``visa_wait_time`` (years). Shared by both scrapers'
    extract_country_data (was duplicated verbatim).
    """
    df = df.copy()
    df["raw_value"] = df[value_col]
    df["status"] = df[value_col].apply(classify_status)
    df[value_col] = df.apply(lambda r: string_to_datetime(r[value_col], r["visa_bulletin_date"]), axis=1)
    df["visa_wait_time"] = df.apply(
        lambda r: (
            (r["visa_bulletin_date"] - r[value_col]).days / 365.25
            if pd.notna(r[value_col]) and pd.notna(r["visa_bulletin_date"])
            else None
        ),
        axis=1,
    )
    return df
