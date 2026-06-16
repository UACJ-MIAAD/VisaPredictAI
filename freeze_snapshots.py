"""Freeze the raw monthly Visa Bulletin HTML to a local immutable snapshot dir.

The scrapers parse live HTML in memory and only persist derived CSVs, so the
true scraping artifact (each month's fixed HTML page) was never saved -- and the
live site already lost ~5 pages to rot (Wayback-only). This grabs each live
bulletin page ONCE and never overwrites: a page already on disk is frozen.

    ante/bin/python freeze_snapshots.py
    aws s3 sync data/snapshots/ s3://<your-bucket>/raw-html/   # then push to S3

ponytail: skip-if-exists IS the immutability -- no versioning logic, no hashing.
The 5 dead-on-live pages won't appear in extract_month_links(); fetch those from
Wayback by hand and drop them in data/snapshots/ once.
"""

import logging
import time
from pathlib import Path

import requests
from tqdm import tqdm

from visa_common import MAX_RETRIES, REQUEST_TIMEOUT, SITE_ROOT, extract_month_links

SNAP_DIR = Path("data/snapshots")
logger = logging.getLogger(__name__)


def fetch_text(url: str) -> str:
    """Raw GET with the same retry+backoff get_soup uses -- a few months hit an
    intermittent redirect loop (e.g. 2007-12) that clears on retry. Kept raw
    (not BeautifulSoup-reserialized) so the snapshot is the wire bytes."""
    last: Exception = RuntimeError(f"no fetch attempt for {url}")
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001 -- mirror get_soup: retry any transient blip
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def main() -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    new = 0
    for link in tqdm(extract_month_links(), desc="Freezing raw HTML"):
        dest = SNAP_DIR / Path(link).name
        if dest.exists():
            continue  # already frozen -- fixed page, never re-fetch
        dest.write_text(fetch_text(SITE_ROOT + link), encoding="utf-8")
        new += 1
    logger.info("%d new snapshots; %d total in %s", new, len(list(SNAP_DIR.glob("*.html"))), SNAP_DIR)
    print(new)  # stdout (logging/tqdm go to stderr) -- the CI step gates rebuild on this count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    main()
