"""Contrato del monitor de drift (base job: solo pandas, sin darts)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_drift", ROOT / "experiments" / "check_drift.py")
assert _spec is not None and _spec.loader is not None
check_drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_drift)


def test_check_returns_wellformed_report(tmp_path) -> None:
    check_drift.REPORTS = tmp_path  # type: ignore[attr-defined]  # H4: NO escribir en el repo real
    try:
        r = check_drift.check()
    finally:
        check_drift.REPORTS = ROOT / "reports"  # type: ignore[attr-defined]
    assert isinstance(r["drift_detected"], bool)
    assert "performance" in r and "data" in r
    # el reporte se escribe en disco (en el tmp del test)
    assert (tmp_path / "governance" / "drift_report.json").exists()


def test_data_drift_flags_have_schema() -> None:
    d = check_drift._data_drift()
    for fl in d.get("flagged", []):
        assert {"series", "delta_days", "hist_median_delta", "mad"} <= set(fl)
