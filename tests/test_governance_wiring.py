"""B263: los gates de gobernanza P0R.5 (commit-frontier, reflexión, safe-opens, raw-fs, recibo B233) deben estar
CABLEADOS como pasos `run:` nombrados en .github/workflows/ci.yml — no sólo correr por transitividad de pytest.
`tools/check_p0r5_governance.py` es una lista positiva: si un paso se elimina del CI, falla nombrando el que falta."""

from __future__ import annotations

import os
import tempfile

import tools.check_p0r5_governance as gov


def test_governance_gates_are_wired_in_ci():
    assert gov.problems() == [], "todos los gates de gobernanza deben estar cableados en ci.yml (B263)"


def test_governance_gate_catches_a_removed_step(monkeypatch):
    # quitar un paso de gate del workflow debe hacer fallar el checker, nombrando el ausente.
    real = open(os.path.join(gov.ROOT, gov._WORKFLOW), encoding="utf-8").read()
    for gate in ("tools/check_reflection.py", "tools/check_commit_frontier.py", "tools.validate_b233_receipt"):
        bad = real.replace(f"python tools/{gate.split('/')[-1]}", "echo removed").replace(
            "python -m tools.validate_b233_receipt", "echo removed"
        )
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".github", "workflows"))
        with open(os.path.join(d, ".github", "workflows", "ci.yml"), "w", encoding="utf-8") as fh:
            fh.write(bad)
        monkeypatch.setattr(gov, "ROOT", d)
        probs = gov.problems()
        assert any(gate in p for p in probs), f"quitar {gate} del CI debe fallar (B263): {probs}"


def test_governance_gate_fail_closed_missing_workflow(monkeypatch):
    monkeypatch.setattr(gov, "ROOT", "/nonexistent_root_p0r5")
    assert gov.problems(), "workflow ausente debe fallar cerrado (B263)"
