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
    bad = _SRC.replace("if not ctx.commit_reached:  # DERIVADO", "if True:  # unguarded", 1)
    assert gate.frontier_problems(bad), "un _rollback() no guardado por commit_reached debe fallar"


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


def test_gate_flags_text_based_commit_decision():
    # decidir el cruce por texto de excepción (str(be)) en vez de authority_crossed debe fallar
    bad = _SRC.replace('getattr(be, "authority_crossed", False) is True', '"COMMIT" in str(be)', 1)
    assert gate.frontier_problems(bad), "decidir el cruce por texto de excepción debe fallar"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
