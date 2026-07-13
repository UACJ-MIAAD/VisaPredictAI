"""Coherencia docs ↔ security/python_advisories.json (P0R.3, ronda 10)."""

from __future__ import annotations

import tools.check_supply_chain_triage as m

_JSON = (
    '{"schema_version":1,"advisories":['
    '{"id":"CVE-2025-3000","aliases":[],"package":"torch","versions":["2.12.0"],'
    '"profiles":["model"],"decision":"accept","owner":"J","expires_at":"2026-08-12"},'
    '{"id":"PYSEC-2026-3043","aliases":["CVE-2026-31221"],"package":"pytorch-lightning",'
    '"versions":["2.5.6"],"profiles":["model"],"decision":"accept","owner":"J","expires_at":"2026-08-12"}]}'
)
_TR = (
    "## Triage (2 avisos en 2 paquetes)\n"
    "| Paquete | Aviso | Decisión |\n|---|---|---|\n"
    "| torch 2.12.0 | CVE-2025-3000 (x) | Accept |\n"
    "| pytorch-lightning 2.5.6 | PYSEC-2026-3043 (alias CVE-2026-31221) | Accept |\n"
)
_TH = "riesgo MEDIO-BAJO: 2 avisos aceptados\n"


def _wire(monkeypatch, tmp_path, js=_JSON, tr=_TR, th=_TH):
    (tmp_path / "j").write_text(js)
    (tmp_path / "tr").write_text(tr)
    (tmp_path / "th").write_text(th)
    monkeypatch.setattr(m, "ADVISORIES", tmp_path / "j")
    monkeypatch.setattr(m, "TRIAGE", tmp_path / "tr")
    monkeypatch.setattr(m, "THREAT", tmp_path / "th")


def test_coherent_passes(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    assert m.main() == 0


def test_prose_count_drift_fails(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, tr=_TR.replace("2 avisos en 2 paquetes", "9 avisos en 7 paquetes"))
    assert m.main() == 1


def test_row_count_mismatch_fails(monkeypatch, tmp_path):
    extra = _TR + "| numpy 2.5 | CVE-2099-9 (x) | Accept |\n"
    _wire(monkeypatch, tmp_path, tr=extra)
    assert m.main() == 1


def test_json_id_without_triage_row_fails(monkeypatch, tmp_path):
    # el JSON referencia un id que no está en ninguna fila de la tabla
    tr = _TR.replace("PYSEC-2026-3043 (alias CVE-2026-31221)", "OTRO-9999 (x)")
    _wire(monkeypatch, tmp_path, tr=tr)
    assert m.main() == 1


def test_real_repo_coherent():
    assert m.main() == 0
