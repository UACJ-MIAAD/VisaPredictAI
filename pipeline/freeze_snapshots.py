"""Freeze the raw monthly Visa Bulletin HTML to a local immutable snapshot dir.

The scrapers parse frozen HTML offline and only persist derived CSVs, so the
true scraping artifact (each month's fixed HTML page) is what this grabs ONCE
and never overwrites: a page already on disk is frozen.

    ante/bin/python -m pipeline.freeze_snapshots
    aws s3 sync data/snapshots/ s3://<your-bucket>/raw-html/   # then push to S3

ponytail: skip-if-exists IS the immutability -- no versioning logic, no hashing.

A1 (fetcher seam): the network lives behind an injected ``vp_data.fetchers``
``Fetcher`` (default: the retrying live one), so this whole module tests
offline. Content validation (``looks_like_bulletin``) runs HERE, in the
consumer, right before the atomic write -- it applies to EVERY fetcher, the
manual-ingestion path included, so a 200 that is really a WAF/maintenance/
soft-404 page is never mummified by skip-if-exists (nor synced to S3, the
source of truth). A page that fails it counts as a failed link for this run
(the cron runs again; no retry loop on content).

A ``SourceBlockedError`` (Cloudflare, live since 2026-08-06) is degradation,
not failure: ``main`` returns ``source_blocked=True`` with ``new`` counting
whatever froze before the block, and the CLI still exits 0 printing the count
-- the cron records the delay instead of going red twice a day.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from vp_data.config import SNAPSHOTS_DIR
from vp_data.fetchers import Fetcher, FetchError, SourceBlockedError, default_fetcher
from vp_data.visa_common import (
    SITE_ROOT,
    extract_datetime_from_link,
    extract_month_links,
    looks_like_bulletin,
    report_failures,
)

SNAP_DIR = SNAPSHOTS_DIR  # single source (vp_data.config); build_database reads the same dir (H2)
# A4: piso conocido del índice de boletines (~298 en jul-2026, solo crece). Si el sitio
# renombra el markup del acordeón, extract_month_links() devuelve [] y el cron se vuelve
# un no-op perpetuo con heartbeat verde "0 nuevos" — abortar ruidosamente en su lugar.
MIN_INDEX_LINKS = 290
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FreezeResult:
    """Outcome of one freeze pass. ``source_blocked`` separates "the source is
    behind its WAF" (degrade: exit clean, record, retry next run) from a real
    failure (starved index, structural break: abort loud)."""

    new: int
    source_blocked: bool = False
    detail: str = ""


def main(fetch: Fetcher | None = None) -> FreezeResult:
    fetch = fetch or default_fetcher()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        links = extract_month_links(fetch=fetch)
    except SourceBlockedError as exc:
        logger.warning("FUENTE BLOQUEADA en el índice: %s — 0 nuevos (retraso, no fallo)", exc)
        return FreezeResult(new=0, source_blocked=True, detail=str(exc))
    if len(links) < MIN_INDEX_LINKS:
        raise SystemExit(
            f"ERROR: el índice de boletines devolvió {len(links)} links (< piso {MIN_INDEX_LINKS}) — "
            "¿cambió el markup de travel.state.gov? Abortando para no volverse un no-op silencioso."
        )
    new = 0
    failed: list[tuple[str, str]] = []  # J5: aislar fallos por link — el primero ya no mata el resto
    blocked_detail: str | None = None
    for link in tqdm(links, desc="Freezing raw HTML"):
        dest = SNAP_DIR / Path(link).name
        if dest.exists():
            continue  # already frozen -- fixed page, never re-fetch
        # J5: an index entry with no mappable month (a special announcement) is
        # not a bulletin — don't even attempt it, and say so.
        if extract_datetime_from_link(link) is None:
            logger.warning("link del índice sin mes mapeable (se omite): %s", link)
            continue
        try:
            content = fetch(SITE_ROOT + link)
        except SourceBlockedError as exc:
            # every remaining link is behind the same WAF: stop burning backoff
            blocked_detail = str(exc)
            break
        except FetchError as exc:
            failed.append((link, str(exc)[:80]))
            continue
        if not looks_like_bulletin(content):
            failed.append((link, "200 sin marcadores de boletín (WAF/mantenimiento?)"))
            continue
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(content)
        os.replace(tmp, dest)  # atomic: never a truncated snapshot on disk
        new += 1
    if blocked_detail is None:
        # J5: same accounting every scraper uses — warn each failed link, abort loud
        # (without printing a count) if so many failed that the source must be broken.
        report_failures(failed, logger)
    else:
        logger.warning("FUENTE BLOQUEADA: %s — se congelaron %d antes del bloqueo", blocked_detail, new)
    logger.info("%d new snapshots; %d total in %s", new, len(list(SNAP_DIR.glob("*.html"))), SNAP_DIR)
    return FreezeResult(new=new, source_blocked=blocked_detail is not None, detail=blocked_detail or "")


def _cli(fetch: Fetcher | None = None) -> None:
    result = main(fetch)
    # stdout (logging/tqdm go to stderr) -- the CI step gates rebuild on `tail -1`
    # being this integer; a blocked source prints 0 and exits clean.
    print(result.new)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    _cli()
