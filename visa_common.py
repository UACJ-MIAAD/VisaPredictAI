"""Shared helpers for the Visa Bulletin scrapers.

Centralizes the fetch / link-discovery / date / status functions that were
byte-for-byte (modulo comments) duplicated across ``scrape_visa_bulletins.py``
and ``scrape_family_visa_bulletins.py``. Each scraper now imports from here and
keeps only what genuinely differs (section detection, category mapping, output
columns).
"""

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
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
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
    """Parse 'visa-bulletin-for-<month>-<year>.html' into a datetime (day=1)."""
    match = re.search(r"visa-bulletin-for-(\w+)-(\d{4})\.html$", link)
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


# ---- cell parsing / annotation -----------------------------------------
def string_to_datetime(date_str: str, bulletin_date: datetime) -> None | datetime:
    """Convert a published cell to a date. 'C' -> bulletin date (legacy
    behavior kept for the wait-time column); 'U'/empty/unparseable -> None."""
    if date_str == "C":
        return bulletin_date
    if date_str == "U" or pd.isna(date_str):
        return None
    try:
        return datetime.strptime(date_str, DATE_FMT)
    except ValueError:
        return None


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
    if s == "C":
        return "C"
    if s == "U":
        return "U"
    try:
        datetime.strptime(str(date_str).strip(), DATE_FMT)
        return "F"
    except ValueError:
        return "UNK"


def norm_label(s) -> str:
    r"""Collapse whitespace noise (\n, \xa0, runs of spaces) and lowercase."""
    if pd.isna(s):
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip().lower()


# Backwards-compatible alias (both scrapers historically used the private name).
_norm_label = norm_label


# ---- table parsing (pure: soup -> dataframes; testable offline) ---------
def parse_tables(soup: BeautifulSoup, year_month, section_matcher) -> list[pd.DataFrame]:
    """Parse the preference tables a ``section_matcher(rows) -> bool`` selects.

    Decoupled from fetching so it can be unit-tested with saved HTML fixtures
    (no network). The first matching table is tagged ``final_action`` and the
    second ``dates_for_filing`` (DFF tables exist only from Oct 2015 on; earlier
    months have a single FAD table). ``section_matcher`` is what makes this
    open to new sections (employment, family, …) without editing the parser.
    """
    dfs = []
    table_count = 0
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not section_matcher(rows):
            continue
        table_count += 1
        table_type = "final_action" if table_count == 1 else "dates_for_filing"

        table_data = []
        for row in rows:
            th_cols = row.find_all("th")
            td_cols = row.find_all("td")
            cols = [ele.text.strip() for ele in th_cols + td_cols]
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

        if table_count >= 2:
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
