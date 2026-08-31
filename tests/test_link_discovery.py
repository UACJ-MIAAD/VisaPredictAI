"""Descubrimiento de links del índice de boletines, offline (A1/A5).

``parse_month_links`` es pura (soup -> hrefs) y ``extract_month_links`` /
``get_soup`` aceptan un ``Fetcher`` inyectado, así que TODO esto corre contra
``tests/fixtures/vb_index_min.html`` sin tocar la red (se aserta que
``requests.get`` no se llama).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from vp_data import fetchers
from vp_data.visa_common import BASE_URL, extract_month_links, get_soup, parse_month_links

FIXTURE = Path(__file__).parent / "fixtures" / "vb_index_min.html"

EXPECTED = [
    "/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-july-2026.html",
    "/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-june-2026.html",
    "/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-may-2026.html",
]


def _no_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("requests.get fue llamado: la red se escapó de la costura")

    monkeypatch.setattr(fetchers.requests, "get", boom)


def test_parse_month_links_is_pure_and_keeps_document_order():
    soup = BeautifulSoup(FIXTURE.read_text(), "html.parser")
    links = parse_month_links(soup)
    assert links == EXPECTED  # el link fuera del acordeón NO entra


def test_extract_month_links_uses_the_injected_fetcher_only(monkeypatch):
    _no_network(monkeypatch)
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return FIXTURE.read_bytes()

    assert extract_month_links(fetch=fetch) == EXPECTED
    assert seen == [BASE_URL]  # una sola petición: la página índice


def test_get_soup_uses_the_injected_fetcher_only(monkeypatch):
    _no_network(monkeypatch)
    soup = get_soup("http://anywhere/page.html", fetch=lambda url: b"<html><p>hola</p></html>")
    assert soup.find("p").text == "hola"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
