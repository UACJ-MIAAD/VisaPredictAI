"""The single network port of the system (A1: DIP seam over the bulletin source).

A ``Fetcher`` is any ``Callable[[str], bytes]`` mapping a URL to page bytes, so
every consumer (``visa_common.get_soup``, ``freeze_snapshots.main``) takes one
injected and is testable offline. This module is the ONLY place allowed to
import ``requests`` (``tests/test_architecture.py::NETWORK_PORTS``), and holds
the ONE retry policy that used to live in three near-identical loops
(``get_soup``, ``freeze_snapshots.fetch_bytes`` and their callers).

``travel.state.gov`` sits behind Cloudflare since 2026-08-06 (403 to every
automated client we measured, browser-UA included), so a fetcher distinguishes
that block from an ordinary 404/500: ``SourceBlockedError`` is permanent —
retrying burns backoff for nothing — and lets the consumer degrade honestly
(freeze exits 0 with ``source_blocked`` instead of failing the cron red).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import requests

from vp_data import config

Fetcher = Callable[[str], bytes]

REQUEST_TIMEOUT = 30
MAX_RETRIES = 6  # a couple of months (e.g. 2007-12) hit an intermittent redirect loop
BACKOFF_BASE_S = 2  # segundos base del backoff lineal del retry

# Cloudflare interstitial/deny markers, measured live on the source (17/26-Aug):
# the WAF page titles itself "Attention Required!", the JS challenge says
# "Just a moment..." and both ship cf-* ids/classes.
_BLOCK_MARKERS = ("just a moment", "attention required", "cf-browser-verification", "cf-please-wait", "cf-error")
_BLOCK_STATUSES = (403, 503)

# Hygiene only: the bare python-requests UA is what the WAF profiles first. A
# real-browser UA alone did NOT unblock the source when measured on 17-Aug —
# these headers just stop making it worse (and are what a legit reader sends).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(Exception):
    """A failed fetch. ``permanent`` tells ``with_retry`` whether retrying can help."""

    def __init__(self, url: str, msg: str, *, status: int | None = None, permanent: bool = False):
        super().__init__(f"{msg} [{url}]")
        self.url = url
        self.status = status
        self.permanent = permanent


class SourceBlockedError(FetchError):
    """The source's WAF/anti-bot layer refused us (Cloudflare). Permanent for
    this run: no amount of backoff clears a challenge page, so consumers must
    degrade (record the block, exit clean) instead of retrying or failing red."""

    def __init__(self, url: str, msg: str = "fuente tras el WAF (Cloudflare)", *, status: int | None = None):
        super().__init__(url, msg, status=status, permanent=True)


def _looks_blocked(status: int, content: bytes, server: str) -> bool:
    if status not in _BLOCK_STATUSES:
        return False
    text = content[:4096].decode("utf-8", errors="replace").lower()
    return "cloudflare" in server.lower() or any(marker in text for marker in _BLOCK_MARKERS)


def requests_fetcher(*, timeout: int = REQUEST_TIMEOUT, headers: dict[str, str] | None = None) -> Fetcher:
    """Live HTTP fetcher returning true wire bytes (``resp.content``: no charset
    re-decode that could mummify mojibake). Raises ``SourceBlockedError`` on a
    WAF block, permanent ``FetchError`` on other 4xx (a month that never
    existed), transient ``FetchError`` on 5xx/network errors."""

    def fetch(url: str) -> bytes:
        try:
            resp = requests.get(url, timeout=timeout, headers=headers or BROWSER_HEADERS)
        except requests.RequestException as exc:
            raise FetchError(url, f"error de red: {exc}") from exc
        if resp.status_code >= 400:
            if _looks_blocked(resp.status_code, resp.content, resp.headers.get("Server", "")):
                raise SourceBlockedError(url, f"HTTP {resp.status_code} tras el WAF", status=resp.status_code)
            raise FetchError(
                url,
                f"HTTP {resp.status_code}",
                status=resp.status_code,
                permanent=400 <= resp.status_code < 500,
            )
        return resp.content

    return fetch


def with_retry(
    fetch: Fetcher,
    *,
    retries: int = MAX_RETRIES,
    backoff_s: float = BACKOFF_BASE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Fetcher:
    """THE retry policy: linear backoff on transient errors, fast-fail on
    permanent ones (a 4xx, a WAF block). ``sleep`` is injectable so tests run
    in milliseconds. Anything that is not a ``FetchError`` propagates: the
    fetcher contract owns error classification, not this wrapper."""

    def fetching(url: str) -> bytes:
        last: FetchError = FetchError(url, "no fetch attempt")
        for attempt in range(retries):
            try:
                return fetch(url)
            except FetchError as exc:
                if exc.permanent:
                    raise
                last = exc
                sleep(backoff_s * (attempt + 1))
        raise last

    return fetching


def local_dir_fetcher(directory: Path | str, fallback: Fetcher | None = None) -> Fetcher:
    """Serve a URL's basename from a local directory (frozen snapshots, the
    manual inbox, test fixtures) — fully offline. A missing file goes to the
    ``fallback`` fetcher, or fails permanent (retrying a local read is noise)."""

    def fetch(url: str) -> bytes:
        candidate = Path(directory) / Path(url).name
        if candidate.is_file():
            return candidate.read_bytes()
        if fallback is not None:
            return fallback(url)
        raise FetchError(url, f"sin archivo local {candidate}", permanent=True)

    return fetch


def default_fetcher() -> Fetcher:
    """Fetcher selected by ``VP_FETCHER`` (default ``requests``):

    * ``requests`` — live HTTP wrapped in the retry policy (production).
    * ``inbox`` — offline: serve ``data/inbox/`` first, then ``data/snapshots/``
      (manually downloaded pages while the source is blocked; zero network).
    * ``browser`` — reserved for the A6 spike (headed browser through the WAF
      challenge). Deliberately NOT implemented yet: failing loud beats
      pretending support.
    """
    kind = os.environ.get("VP_FETCHER", "requests")
    if kind == "requests":
        return with_retry(requests_fetcher())
    if kind == "inbox":
        return local_dir_fetcher(config.INBOX_DIR, fallback=local_dir_fetcher(config.SNAPSHOTS_DIR))
    if kind == "browser":
        raise NotImplementedError(
            "VP_FETCHER=browser está diferido al spike A6 (navegador headed); usa 'requests' o 'inbox'"
        )
    raise ValueError(f"VP_FETCHER desconocido: {kind!r} (opciones: requests, inbox)")
