"""Shared helpers for the Visa Bulletin scrapers.

Centralizes the fetch / link-discovery / date / status functions that were
byte-for-byte (modulo comments) duplicated across ``scrape_visa_bulletins.py``
and ``scrape_family_visa_bulletins.py``. Each scraper now imports from here and
keeps only what genuinely differs (section detection, category mapping, output
columns).
"""
from typing import List, Union
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---- configuration (single source of truth) ----------------------------
BASE_URL = "https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin.html"
SITE_ROOT = "https://travel.state.gov/"
# Countries scraped, in the order the per-country CSVs are written. 'row' =
# "All Chargeability Areas Except Those Listed".
SCRAPER_COUNTRIES = ["india", "china", "mexico", "philippines", "row"]
DATE_FMT = "%d%b%y"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


# ---- link discovery -----------------------------------------------------
def extract_datetime_from_link(link: str) -> Union[None, datetime]:
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
    last = None
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def extract_month_links() -> List[str]:
    """Return every monthly-bulletin href listed in the page accordion."""
    soup = get_soup(BASE_URL)
    month_links = []
    for section in soup.find_all("div", class_="accordion parbase section"):
        link_container = section.find("div", class_="tsg-rwd-accordion-copy")
        if link_container:
            for link in link_container.find_all("a", href=True):
                month_links.append(link["href"])
    return month_links


# ---- cell parsing / annotation -----------------------------------------
def string_to_datetime(date_str: str, bulletin_date: datetime) -> Union[None, datetime]:
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
