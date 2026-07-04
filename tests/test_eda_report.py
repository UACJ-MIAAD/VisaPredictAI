"""Smoke test del reporte EDA (``reports/eda/eda_report.pdf``).

Valida el ARTEFACTO versionado sin regenerarlo: existe, es un PDF con las páginas
esperadas, pesa < 3 MB (hook large-files) y su vintage coincide con eda_facts.json.
La regeneración vive en el cron (`make eda-all && make eda-report`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "reports" / "eda" / "eda_report.pdf"
PDF_EN = ROOT / "reports" / "eda" / "en" / "eda_report.pdf"
FACTS = ROOT / "reports" / "eda" / "eda_facts.json"
MIN_PAGES = 14  # portada + resumen + 11 figuras + notas


def test_report_exists_and_fits_budget():
    # PENDIENTES #14: la variante EN es parte del contrato (la sirve la página /en del sitio)
    for pdf in (PDF, PDF_EN):
        assert pdf.exists(), f"falta {pdf.relative_to(ROOT)} (corre `make eda-report`)"
        size = pdf.stat().st_size
        assert 0 < size < 3_000_000, f"{pdf.name}: {size / 1e6:.1f} MB fuera de presupuesto (<3 MB)"
        assert pdf.read_bytes().startswith(b"%PDF-"), f"{pdf.name} no es un PDF"


def test_report_structure_and_vintage():
    # Sin deps: los PDF de matplotlib llevan el conteo en "/Count N" (objeto Pages)
    # y los metadatos como literales; el parseo por bytes basta para el contrato.
    import re

    raw = PDF.read_bytes()
    assert raw.startswith(b"%PDF-"), "no es un PDF"
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", raw)]
    assert counts and max(counts) >= MIN_PAGES, f"páginas {counts} < {MIN_PAGES}"
    facts = json.loads(FACTS.read_text())
    # el título del PDF lleva el corte; debe ser el MISMO vintage que el censo (regla #0).
    # matplotlib lo serializa como literal `(...)` o como hex UTF-16 `<FEFF...>`.
    m = re.search(rb"/Title \((.*?)\)\s*/", raw, re.S)
    body = m.group(1).replace(b"\\(", b"(").replace(b"\\)", b")") if m else b""
    title = body.decode("utf-16") if body.startswith(b"\xfe\xff") else body.decode("latin-1")
    year = facts["vintage"][:4]
    assert year in title, f"vintage {facts['vintage']} ausente del título del PDF: {title!r}"


if __name__ == "__main__":
    test_report_exists_and_fits_budget()
    test_report_structure_and_vintage()
    print("OK — eda_report.pdf ES+EN presentes, dentro de presupuesto y con vintage alineado")
    sys.exit(0)
