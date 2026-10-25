"""Executed PDF intake contracts, including real Poppler extraction when supplied."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from pipeline import ingest_pdf
from pipeline.ingest_manual import _completeness_problems
from pipeline.ingest_pdf import merge_month
from pipeline.pdf_bulletin import as_html, extract_text, parse_layout

FIXTURE = Path(__file__).parent / "fixtures/pdf_bulletin_november_2026.txt"


def test_extracted_real_layout_has_exact_cells_and_typed_c():
    cells = parse_layout(FIXTURE.read_text(), "2026-11")
    assert len(cells) == 150
    assert sum(c["value"] == "C" for c in cells) == 47
    assert sum(c["value"] not in {"C", "U"} for c in cells) == 103
    assert not _completeness_problems(
        BeautifulSoup(as_html(cells, "2026-11", "0" * 64), "html.parser"), datetime(2026, 11, 1)
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t.replace("November 2026", "October 2026"),
        lambda t: t.replace("F1           22JAN21", "F0           22JAN21"),
        lambda t: t.replace("F1           22JAN21         22JAN21 22JAN21 22SEP08 01NOV13", ""),
        lambda t: t.replace("22SEP08", "99SEP08"),
        lambda t: t.replace("INDIA       MEXICO", "MEXICO       INDIA"),
    ],
)
def test_ambiguous_or_changed_source_fails_closed(change):
    text = FIXTURE.read_text()
    altered = change(text)
    assert altered != text
    with pytest.raises(ValueError):
        parse_layout(altered, "2026-11")


def test_u_is_not_fabricated_as_a_date():
    cells = parse_layout(FIXTURE.read_text().replace("22SEP08", "U"), "2026-11")
    assert sum(c["value"] == "U" for c in cells) == 1


def test_existing_month_is_idempotent_and_conflict_is_rejected():
    old = pd.DataFrame(
        [dict(EB_level="EB1", table_type="final_action", raw_value="C", status="C", visa_bulletin_date="2026-11-01")]
    )
    assert merge_month(old, old.copy(), "2026-11") is old
    with pytest.raises(ValueError, match="congelado"):
        merge_month(old, old.assign(raw_value="U", status="U"), "2026-11")


@pytest.mark.parametrize(("name", "month"), [("October", "2026-10"), ("November", "2026-11")])
def test_real_pdf_extraction(name, month):
    folder = FIXTURE.parent / "pdf_bulletins"
    text, version = extract_text(folder / f"Visa Bulletin For {name} 2026.pdf")
    assert "pdftotext" in version
    assert len(parse_layout(text, month)) == 150


def test_month_format_and_inbox_ambiguity_fail_without_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_pdf, "ROOT", tmp_path)
    inbox = tmp_path / "data/inbox"
    inbox.mkdir(parents=True)
    for name in ("one.pdf", "two.pdf"):
        (inbox / name).write_bytes(b"%PDF-not-real")
    with pytest.raises(ValueError, match="YYYY-MM"):
        ingest_pdf.main(["--month", "2026-1"])
    with pytest.raises(ValueError, match="exactamente un PDF"):
        ingest_pdf.main(["--month", "2026-11"])
    assert not (tmp_path / "data/pdf_intake").exists()


def test_parse_failure_archives_original_and_preserves_live_outputs(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(ingest_pdf, "ROOT", tmp_path)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-original")
    live = tmp_path / "data/processed/visa_panel_long.csv"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original panel")

    def bad_text(path):
        assert path.name == "source.pdf"
        assert path.read_bytes() == b"%PDF-original"
        return "no bulletin matrices", "mock Poppler"

    monkeypatch.setattr(ingest_pdf, "extract_text", bad_text)
    with pytest.raises(ValueError):
        ingest_pdf.main(["--file", str(source), "--month", "2026-11"])
    receipt = json.loads(next((tmp_path / "data/pdf_intake").rglob("receipt.json")).read_text())
    assert receipt["status"] == "failed"
    assert receipt["candidate_promoted"] is False
    assert receipt["publication"] == "not_requested"
    assert live.read_bytes() == b"original panel"


def test_historical_snapshot_cache_missing_blocks_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_pdf, "ROOT", tmp_path)
    (tmp_path / "data/raw").mkdir(parents=True)
    (tmp_path / "data/processed").mkdir()
    pd.DataFrame({"bulletin_date": ["2026-10-01"]}).to_csv(tmp_path / "data/processed/visa_panel_long.csv", index=False)
    stage = tmp_path / "candidate"
    stage.mkdir()
    with pytest.raises(ValueError, match="histórica incompleta"):
        ingest_pdf.build_candidate(b"unused", "2026-11", stage, tmp_path, "test")
    assert not (stage / "data/processed/visapredict.duckdb").exists()
