"""Correspondencia allowlist ↔ triage ↔ docs de supply chain (P0R, ronda 10)."""

from __future__ import annotations

import tools.check_supply_chain_triage as m

_WF = "            --ignore-vuln CVE-2025-3000\n            --ignore-vuln PYSEC-2026-3043\n            # retirar su --ignore-vuln aquí Y su fila\n"
_TR = (
    "## Triage vigente — perfil model (2 avisos en 2 paquetes)\n"
    "| Paquete | Aviso | Decisión |\n"
    "|---|---|---|\n"
    "| torch 2.12.0 | CVE-2025-3000 (sin fix) | Accept |\n"
    "| pytorch-lightning 2.5.6 | PYSEC-2026-3043 (alias CVE-2026-31221) | Accept |\n"
)
_TH = "riesgo MEDIO-BAJO: 2 avisos aceptados del perfil model (torch/lightning)\n"


def test_ignores_extraction_excludes_prose():
    # "--ignore-vuln aquí" en un comentario NO cuenta (no tiene forma de advisory)
    assert m._ignores(_WF) == ["CVE-2025-3000", "PYSEC-2026-3043"]


def _wire(monkeypatch, tmp_path, wf=_WF, tr=_TR, th=_TH):
    (tmp_path / "wf").write_text(wf)
    (tmp_path / "tr").write_text(tr)
    (tmp_path / "th").write_text(th)
    monkeypatch.setattr(m, "WORKFLOW", tmp_path / "wf")
    monkeypatch.setattr(m, "TRIAGE", tmp_path / "tr")
    monkeypatch.setattr(m, "THREAT", tmp_path / "th")


def test_coherent_passes(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    assert m.main() == 0


def test_prose_count_drift_fails(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, tr=_TR.replace("2 avisos en 2 paquetes", "9 avisos en 7 paquetes"))
    assert m.main() == 1


def test_workflow_ignore_without_triage_row_fails(monkeypatch, tmp_path):
    extra = _WF.replace("# retirar", "--ignore-vuln CVE-2099-9999\n            # retirar")
    _wire(monkeypatch, tmp_path, wf=extra)
    assert m.main() == 1


def test_real_repo_files_are_coherent():
    # el estado REAL del repo debe estar coherente (2 avisos)
    assert m.main() == 0
