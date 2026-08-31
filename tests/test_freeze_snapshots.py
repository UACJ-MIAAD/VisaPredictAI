"""``pipeline.freeze_snapshots`` tras la costura A1: sin red, con Fetcher inyectado.

Sustituye la mitad *freeze* de ``tests/test_web_publish.py`` (que monkeypatcheaba
``freeze_snapshots.requests``/``fetch_bytes``, hoy inexistentes): el contrato de
stdout del cron (``tail -1`` = entero), el gate del índice hambriento, la
degradación honesta ante la fuente bloqueada (exit 0 + ``source_blocked``) y la
validación de contenido en el consumidor (aplica a TODO fetcher, manual incluido).
"""

from __future__ import annotations

import pytest

from pipeline import freeze_snapshots
from pipeline.freeze_snapshots import FreezeResult
from vp_data.fetchers import FetchError, SourceBlockedError
from vp_data.visa_common import BASE_URL, looks_like_bulletin

BULLETIN = b"<html><h1>Visa Bulletin for July 2026</h1><table>All Chargeability Areas</table></html>"


# ------------------------------------------------------------ looks_like_bulletin
def test_looks_like_bulletin_accepts_real_and_rejects_junk():
    assert looks_like_bulletin(BULLETIN)
    # WAF/mantenimiento: sin 'chargeability' aunque el template del sitio diga Visa Bulletin
    assert not looks_like_bulletin(b"<html><title>Access Denied</title>Reference #18</html>")
    assert not looks_like_bulletin(b"<nav>Visa Bulletin</nav><h1>Page not found</h1>")
    # bytes no-UTF8 no deben explotar (errors='replace')
    assert not looks_like_bulletin(b"\xff\xfe garbage")


# ----------------------------------------------------------------------- helpers
def _index_fetch(names: list[str], pages: dict[str, bytes | Exception] | None = None):
    """Fetcher sintético: sirve el índice del acordeón para BASE_URL y las
    páginas dadas para cada link; lo demás es un 404 permanente."""
    links = "".join(f'<a href="/vb/{n}">x</a>' for n in names)
    index = f'<div class="accordion parbase section"><div class="tsg-rwd-accordion-copy">{links}</div></div>'

    def fetch(url: str) -> bytes:
        if url == BASE_URL:
            return index.encode()
        name = url.rsplit("/", 1)[-1]
        if pages and name in pages:
            page = pages[name]
            if isinstance(page, Exception):
                raise page
            return page
        raise FetchError(url, "HTTP 404", status=404, permanent=True)

    return fetch


def _frozen_names(n: int) -> list[str]:
    months = [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
    return [f"visa-bulletin-for-{months[i % 12]}-{2000 + i // 12}.html" for i in range(n)]


@pytest.fixture()
def snap_dir(tmp_path, monkeypatch):
    d = tmp_path / "snapshots"
    d.mkdir()
    monkeypatch.setattr(freeze_snapshots, "SNAP_DIR", d)
    return d


# ------------------------------------------------------------------------- main
def test_main_aborts_on_starved_index(snap_dir):
    # A4: un índice con menos links que el piso conocido = markup cambiado → abortar
    with pytest.raises(SystemExit):
        freeze_snapshots.main(fetch=_index_fetch(["visa-bulletin-for-july-2026.html"]))


def test_main_returns_zero_when_everything_is_already_frozen(snap_dir):
    names = _frozen_names(freeze_snapshots.MIN_INDEX_LINKS)
    for n in names:
        (snap_dir / n).write_bytes(b"x")
    result = freeze_snapshots.main(fetch=_index_fetch(names))
    assert result == FreezeResult(new=0)
    assert not result.source_blocked


def test_main_freezes_a_new_valid_month_atomically(snap_dir):
    names = _frozen_names(freeze_snapshots.MIN_INDEX_LINKS + 1)
    for n in names[:-1]:
        (snap_dir / n).write_bytes(b"x")
    result = freeze_snapshots.main(fetch=_index_fetch(names, pages={names[-1]: BULLETIN}))
    assert result.new == 1 and not result.source_blocked
    assert (snap_dir / names[-1]).read_bytes() == BULLETIN
    assert not list(snap_dir.glob("*.part"))  # escritura atómica: sin restos


def test_main_rejects_content_without_bulletin_markers(snap_dir, caplog):
    # la validación vive en el CONSUMIDOR: un 200 de mantenimiento no se momifica
    names = _frozen_names(freeze_snapshots.MIN_INDEX_LINKS + 1)
    for n in names[:-1]:
        (snap_dir / n).write_bytes(b"x")
    with caplog.at_level("WARNING"):
        result = freeze_snapshots.main(fetch=_index_fetch(names, pages={names[-1]: b"<html>maintenance</html>"}))
    assert result.new == 0
    assert not (snap_dir / names[-1]).exists()


def test_main_degrades_honestly_when_the_index_is_blocked(snap_dir, caplog):
    def blocked(url: str) -> bytes:
        raise SourceBlockedError(url, "HTTP 403 tras el WAF", status=403)

    with caplog.at_level("WARNING"):
        result = freeze_snapshots.main(fetch=blocked)
    assert result == FreezeResult(new=0, source_blocked=True, detail=result.detail)
    assert result.detail  # el motivo viaja al consumidor (cron/estado de ingesta)
    assert "FUENTE BLOQUEADA" in caplog.text


def test_main_stops_the_loop_on_first_blocked_page_and_keeps_what_froze(snap_dir):
    names = _frozen_names(freeze_snapshots.MIN_INDEX_LINKS + 2)
    for n in names[:-2]:
        (snap_dir / n).write_bytes(b"x")
    pages = {
        names[-2]: BULLETIN,
        names[-1]: SourceBlockedError("http://x", "HTTP 403 tras el WAF", status=403),
    }
    result = freeze_snapshots.main(fetch=_index_fetch(names, pages=pages))
    assert result.source_blocked and result.new == 1  # lo congelado antes del bloqueo se queda
    assert (snap_dir / names[-2]).exists()


def test_main_skips_index_entries_without_a_mappable_month(snap_dir, caplog):
    names = _frozen_names(freeze_snapshots.MIN_INDEX_LINKS)
    for n in names:
        (snap_dir / n).write_bytes(b"x")
    with caplog.at_level("WARNING"):
        result = freeze_snapshots.main(fetch=_index_fetch([*names, "update-on-july-visa-availability.html"]))
    assert result.new == 0
    assert "sin mes mapeable" in caplog.text


# ------------------------------------------------------------------ CLI contract
def test_cli_stdout_contract_is_last_line_int(snap_dir, capsys):
    # el gate del cron lee `tail -1` del stdout: DEBE ser un entero
    names = _frozen_names(freeze_snapshots.MIN_INDEX_LINKS)
    for n in names:
        (snap_dir / n).write_bytes(b"x")
    freeze_snapshots._cli(fetch=_index_fetch(names))
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "0"


def test_cli_prints_zero_and_exits_clean_when_source_is_blocked(snap_dir, capsys):
    def blocked(url: str) -> bytes:
        raise SourceBlockedError(url, "HTTP 403 tras el WAF", status=403)

    freeze_snapshots._cli(fetch=blocked)  # NO SystemExit: bloqueo = degradación, no fallo
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "0"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
