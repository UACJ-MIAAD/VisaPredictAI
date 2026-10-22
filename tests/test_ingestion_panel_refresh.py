"""A manual rebuild advances the panel, not the source-success history."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tools import check_ingestion as gate


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    previous = {
        "schema": 1,
        "status": "blocked",
        "panel_vintage": "2026-09",
        "expected_month": "2026-09",
        "missing_months": [],
        "status_since": "2026-09-02",
        "last_success_date": None,
        "n_index_links": None,
        "failed_links": [],
        "reason": "HTTP 403 tras el WAF",
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(previous))
    panel = tmp_path / "panel.csv"
    panel.write_text("bulletin_date\n2026-10-01\n")
    monkeypatch.setattr(gate, "STATE", path)
    monkeypatch.setattr(gate, "PANEL", panel)
    return path, previous


def test_manual_rebuild_preserves_source_observation_and_updates_panel_calendar(state: tuple[Path, dict]) -> None:
    path, previous = state
    assert gate.refresh_panel_status(date(2026, 10, 22))
    actual = json.loads(path.read_text())
    assert actual["panel_vintage"] == "2026-10"
    assert actual["expected_month"] == "2026-11"  # calendar expectation, not publication evidence
    assert actual["missing_months"] == ["2026-11"]
    for key in ["status", "status_since", "last_success_date", "reason", "n_index_links", "failed_links"]:
        assert actual[key] == previous[key]
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert not gate.refresh_panel_status(date(2026, 10, 22))
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_absent_observation_is_not_invented(state: tuple[Path, dict]) -> None:
    path, _ = state
    path.unlink()
    assert not gate.refresh_panel_status(date(2026, 10, 22))
    assert not path.exists()


def test_corrupt_observation_is_preserved_and_rejected(state: tuple[Path, dict]) -> None:
    path, _ = state
    path.write_text("{broken")
    with pytest.raises(ValueError):
        gate.refresh_panel_status(date(2026, 10, 22))
    assert path.read_text() == "{broken"


def test_regressed_panel_is_rejected(state: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = state
    before = path.read_bytes()
    gate.PANEL.write_text("bulletin_date\n2026-08-01\n")
    with pytest.raises(ValueError, match="retrocede"):
        gate.refresh_panel_status(date(2026, 10, 22))
    assert path.read_bytes() == before


def test_cli_refresh_updates_only_the_recorded_state(state: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = state
    monkeypatch.setattr("sys.argv", ["check_ingestion.py", "--mode", "refresh-panel"])
    assert gate.main() == 0
    assert json.loads(path.read_text())["panel_vintage"] == "2026-10"
