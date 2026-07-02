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

A1 hardening: every candidate page must pass _looks_like_bulletin() BEFORE it is
written -- a 200 that is really a WAF/maintenance/soft-404 page would otherwise be
mummified forever by skip-if-exists (and synced to S3, the source of truth).
Writes are wire bytes (resp.content, no re-decode) and atomic (tmp + os.replace),
so a killed run never leaves a truncated snapshot that skip-if-exists protects.
"""

import logging
import os
import time
from pathlib import Path

import requests
from tqdm import tqdm

from visa_common import MAX_RETRIES, REQUEST_TIMEOUT, SITE_ROOT, extract_month_links

SNAP_DIR = Path("data/snapshots")
# A4: piso conocido del índice de boletines (~298 en jul-2026, solo crece). Si el sitio
# renombra el markup del acordeón, extract_month_links() devuelve [] y el cron se vuelve
# un no-op perpetuo con heartbeat verde "0 nuevos" — abortar ruidosamente en su lugar.
MIN_INDEX_LINKS = 290
logger = logging.getLogger(__name__)


def _looks_like_bulletin(content: bytes) -> bool:
    """Cheap sanity gate before freezing a page forever.

    Empirical over the full 25-year archive (298 snapshots): every real monthly
    bulletin carries BOTH markers; the site-wide nav template does mention
    "Visa Bulletin", so a soft-404/maintenance page could carry that one, but
    never "chargeability". Only known exception:
    update-on-july-visa-availability.html (2007 special announcement, already
    frozen -- skip-if-exists means it is never re-fetched or re-validated).
    """
    t = content.decode("utf-8", errors="replace").lower()
    return "visa bulletin" in t and "chargeability" in t


def fetch_bytes(url: str) -> bytes:
    """Raw GET with the same retry+backoff get_soup uses -- a few months hit an
    intermittent redirect loop (e.g. 2007-12) that clears on retry. Returns
    resp.content (true wire bytes: no charset re-decode that could momify
    mojibake). Content that fails _looks_like_bulletin() is treated as a
    transient failure (WAF page) and retried; after MAX_RETRIES it raises, so
    the Action fails LOUD instead of freezing garbage."""
    last: Exception = RuntimeError(f"no fetch attempt for {url}")
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            if not _looks_like_bulletin(resp.content):
                raise ValueError(f"200 sin marcadores de boletín (WAF/mantenimiento?): {url}")
            return resp.content
        except Exception as exc:  # noqa: BLE001 -- mirror get_soup: retry any transient blip
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def main() -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    links = extract_month_links()
    if len(links) < MIN_INDEX_LINKS:
        raise SystemExit(
            f"ERROR: el índice de boletines devolvió {len(links)} links (< piso {MIN_INDEX_LINKS}) — "
            "¿cambió el markup de travel.state.gov? Abortando para no volverse un no-op silencioso."
        )
    new = 0
    for link in tqdm(links, desc="Freezing raw HTML"):
        dest = SNAP_DIR / Path(link).name
        if dest.exists():
            continue  # already frozen -- fixed page, never re-fetch
        content = fetch_bytes(SITE_ROOT + link)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(content)
        os.replace(tmp, dest)  # atomic: never a truncated snapshot on disk
        new += 1
    logger.info("%d new snapshots; %d total in %s", new, len(list(SNAP_DIR.glob("*.html"))), SNAP_DIR)
    print(new)  # stdout (logging/tqdm go to stderr) -- the CI step gates rebuild on this count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    main()
