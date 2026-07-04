"""Smoke test del reporte FE bilingüe y de su galería (épica AF3).

Valida los ARTEFACTOS versionados sin regenerarlos: los dos PDF (ES y EN) existen,
son PDF con las páginas esperadas, pesan < 3 MB, sus stats embebidos (Keywords)
COINCIDEN con fe_facts.json (regla #0, leído del JSON — nada a mano) y la galería
está completa en sus 4 variantes idioma × tema (mismo set de 7 PNG en cada dir).
La regeneración vive en el cron (`make fe-all`).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE_DIR = ROOT / "reports" / "fe"
PDFS = {"es": FE_DIR / "fe_report.pdf", "en": FE_DIR / "en" / "fe_report.pdf"}
FACTS = FE_DIR / "fe_facts.json"
GALLERY = FE_DIR / "gallery"
MIN_PAGES = 15  # portada + 2 limpieza + 2 FE + 7 figuras + ledger + selección + notas
N_FIGS = 7  # f01..f07


def _meta_literal(raw: bytes, key: bytes) -> str:
    # el `(?=/|>>)` tras el literal hace backtracking sobre los `\)` escapados del
    # texto Y tolera que la clave sea la última del diccionario (sin `/` siguiente)
    m = re.search(rb"/" + key + rb" \((.*?)\)\s*(?=/|>>)", raw, re.S)
    body = m.group(1).replace(b"\\(", b"(").replace(b"\\)", b")") if m else b""
    return body.decode("utf-16") if body.startswith(b"\xfe\xff") else body.decode("latin-1")


def test_reports_exist_and_fit_budget():
    for lang, pdf in PDFS.items():
        assert pdf.exists(), f"falta {pdf} (corre `make fe-report`) [{lang}]"
        size = pdf.stat().st_size
        assert 0 < size < 3_000_000, f"{size / 1e6:.1f} MB fuera de presupuesto (<3 MB) [{lang}]"


def test_reports_structure_and_stats():
    """Páginas mínimas + los stats embebidos en Keywords == fe_facts (regla #0)."""
    facts = json.loads(FACTS.read_text())
    led = facts["cleaning_ledger"]
    fs = facts["feature_selection"]
    for lang, pdf in PDFS.items():
        raw = pdf.read_bytes()
        assert raw.startswith(b"%PDF-"), f"no es un PDF [{lang}]"
        counts = [int(m) for m in re.findall(rb"/Count (\d+)", raw)]
        assert counts and max(counts) >= MIN_PAGES, f"páginas {counts} < {MIN_PAGES} [{lang}]"
        title = _meta_literal(raw, b"Title")
        assert facts["vintage"][:4] in title, f"vintage {facts['vintage']} ausente del título: {title!r} [{lang}]"
        kw = dict(part.split("=") for part in _meta_literal(raw, b"Keywords").split("; "))
        assert kw["vintage"] == facts["vintage"], f"Keywords vintage {kw} [{lang}]"
        assert int(kw["n_rows"]) == int(led["n_rows"]), f"n_rows {kw['n_rows']} != ledger [{lang}]"
        assert int(kw["n_series"]) == int(led["n_series"]), f"n_series {kw['n_series']} != ledger [{lang}]"
        assert int(kw["rows_F"]) == int(led["rows_by_status"]["F"]), f"rows_F {kw['rows_F']} != ledger [{lang}]"
        assert int(kw["features_in"]) == int(fs["n_features_in"]), f"features_in {kw} [{lang}]"
        assert int(kw["selected"]) == int(fs["n_selected"]), f"selected {kw} [{lang}]"


def test_gallery_four_variants_complete():
    """El mismo set de 7 PNG f0*.png en las 4 variantes (28 en total)."""
    base = {p.name for p in GALLERY.glob("*.png")}
    assert len(base) == N_FIGS, f"galería base: {sorted(base)} (esperados {N_FIGS})"
    assert all(re.match(r"f0[1-7]_", n) for n in base), f"nombres fuera de contrato: {sorted(base)}"
    total = len(base)
    for sub in ("dark", "en", "en/dark"):
        names = {p.name for p in (GALLERY / sub).glob("*.png")}
        assert names == base, f"gallery/{sub} difiere del set base: {sorted(base ^ names)}"
        total += len(names)
    assert total == 4 * N_FIGS, f"{total} PNG != {4 * N_FIGS}"


if __name__ == "__main__":
    test_reports_exist_and_fit_budget()
    test_reports_structure_and_stats()
    test_gallery_four_variants_complete()
    print("OK — fe_report ES+EN presentes, stats alineados a fe_facts y galería 4×7 completa")
    sys.exit(0)
