"""Gate ESTRUCTURAL de la frontera de commit (P0R.5 · Incremento 2). La autoridad del commit es el CommitCertificate
de CURRENT; el recibo es evidencia. El gate lo enforce estáticamente sobre `tools/merge_campaign_pools.py`."""

from __future__ import annotations

import pathlib

import pytest

import tools.check_commit_frontier as gate
import tools.merge_campaign_pools as mcp

_SRC = pathlib.Path(mcp.__file__).read_text()


def test_commit_frontier_is_intact():
    assert gate.main() == 0


def test_gate_flags_commit_reached_assignment():
    bad = _SRC.replace("self._committed = True", "self.commit_reached = True", 1)
    assert gate.frontier_problems(bad), "un commit_reached asignado debe fallar (es property derivada)"


def test_gate_flags_second_commit_point():
    bad = _SRC.replace("ctx.mark_current_certified(cert)", "ctx.mark_current_certified(cert)\n        ctx.mark_current_certified(cert)", 1)  # fmt: skip
    assert gate.frontier_problems(bad), "un segundo mark_current_certified debe fallar (commit único)"


def test_gate_flags_unguarded_rollback():
    bad = _SRC.replace("if ctx.rollback_allowed:  # B221", "if True:  # unguarded", 1)
    assert gate.frontier_problems(bad), "un _rollback() no guardado por rollback_allowed debe fallar"


def test_gate_flags_receipt_touching_committed_state():
    # inyectar un mark_current_certified DENTRO de _certify_receipt (el recibo declarando commit) debe fallar
    bad = _SRC.replace(
        "    # Incremento 2: el recibo es EVIDENCIA revalidada",
        "    ctx.mark_current_certified(None)\n    # Incremento 2: el recibo es EVIDENCIA revalidada",
        1,
    )
    probs = gate.frontier_problems(bad)
    assert any("mark_current_certified" in p or "recibo" in p or "estado comprometido" in p for p in probs), (
        "el recibo tocando el estado comprometido debe fallar"
    )


def test_gate_flags_getattr_authority_crossed():
    # B222/B223: clasificar el cruce por `getattr(x, "authority_crossed")` (duck typing) debe fallar.
    bad = _SRC.replace(
        "        _validate_commit_certificate(certificate)",
        '        getattr(certificate, "authority_crossed", False)',
        1,
    )
    probs = gate.frontier_problems(bad)
    assert any("authority_crossed" in p for p in probs), "getattr(authority_crossed) debe fallar"


def test_gate_flags_rollback_not_guarded_by_rollback_allowed():
    # B221: el rollback debe estar guardado por `rollback_allowed` (no `commit_reached`, que ignora el indeterminado).
    bad = _SRC.replace("if ctx.rollback_allowed:  # B221", "if not ctx.commit_reached:  # DEBILITADO", 1)
    assert gate.frontier_problems(bad), (
        "un rollback guardado sólo por commit_reached debe fallar (ignora indeterminado)"
    )


def test_gate_flags_missing_indeterminate_terminal():
    # B221: el terminal AUTHORITY_INDETERMINATE y mark_indeterminate deben existir.
    bad = _SRC.replace("_S_AUTHORITY_INDETERMINATE", "_S_REMOVED_XX")
    assert gate.frontier_problems(bad), "quitar el terminal AUTHORITY_INDETERMINATE debe fallar"


def test_gate_flags_missing_certificate_validation():
    # B222: mark_current_certified debe validar con _validate_commit_certificate (no aceptar cualquier objeto).
    bad = _SRC.replace(
        "        _validate_commit_certificate(certificate)  # fail-closed", "        pass  # sin validar", 1
    )
    assert gate.frontier_problems(bad), "mark_current_certified sin _validate_commit_certificate debe fallar"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
