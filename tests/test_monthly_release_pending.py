"""An unsealed new month must not be abandoned by the next scheduled run."""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from tools import check_ingestion as gate


@pytest.mark.parametrize("release", ["2026-09", None, "2026-10"])
def test_cli_considers_the_last_sealed_vintage(tmp_path: Path, monkeypatch, capsys, release: str | None) -> None:
    for relative, body in (
        ("reports/governance/key_facts.json", {"date_last": "2026-10"}),
        ("reports/eda/eda_facts.json", {"vintage": "2026-10"}),
    ):
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body))
    if release:
        p = tmp_path / "reports/release/release_manifest.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"panel_vintage": release}))
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "panel_max", lambda: pd.Timestamp("2026-10-01"))
    monkeypatch.setattr(sys, "argv", ["check_ingestion.py", "--mode", "model-pending"])
    assert gate.main() == 0
    assert capsys.readouterr().out.strip() == ("fresh" if release == "2026-10" else "stale")
