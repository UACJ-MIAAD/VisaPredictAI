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
    bad = _SRC.replace("ctx.mark_current_certified(cert, expected_campaign=campaign)", "ctx.mark_current_certified(cert, expected_campaign=campaign)\n        ctx.mark_current_certified(cert, expected_campaign=campaign)", 1)  # fmt: skip
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
        "_validate_commit_certificate(certificate, expected_campaign=expected_campaign)",
        'getattr(certificate, "authority_crossed", False)',
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
        "_validate_commit_certificate(certificate, expected_campaign=expected_campaign)  # fail-closed + evidencia",
        "pass  # sin validar",
        1,
    )
    assert gate.frontier_problems(bad), "mark_current_certified sin _validate_commit_certificate debe fallar"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_gate_flags_certificate_without_semantic_fields():
    # B226: un _validate_commit_certificate reducido a tipo+durabilidad+hashes (sin los campos SEMANTICOS:
    # previous_bundle_id / campaign_id / inodes) debe fallar el gate — o un cert real con basura ahi pasaria.
    bad = _SRC
    for f in ("previous_bundle_id", "campaign_id", "pointer_inode", "bundle_inode"):
        bad = bad.replace(f"certificate.{f}", "certificate.bundle_id")
    probs = gate.frontier_problems(bad)
    assert any("semántico" in p or "B226" in p for p in probs), "un cert-validator sin campos semanticos debe fallar"


def test_gate_flags_certificate_built_outside_factory():
    # B231: construir CommitCertificate FUERA de _build_certificate (en campaign_bundle) debe fallar la fabrica-gate.
    import pathlib

    import tools.campaign_bundle as cb

    cb_src = pathlib.Path(cb.__file__).read_text()
    bad = cb_src.replace(
        "def _validate_authority(camp_fd: int, bundle_id: str) -> None:",
        "def _sneak():\n    return CommitCertificate(bundle_id='a')\n\n\ndef _validate_authority(camp_fd: int, bundle_id: str) -> None:",
        1,
    )
    assert gate.factory_problems(bad), "un cert construido fuera de _build_certificate debe fallar"
    assert not gate.factory_problems(cb_src), "el codigo real debe pasar la fabrica-gate"


def test_gate_flags_merge_constructing_certificate():
    # B231: el merge NUNCA construye un CommitCertificate (solo consume el de la fabrica).
    bad = _SRC.replace(
        "def _validate_commit_certificate(",
        "def _sneak2():\n    return _bundle.CommitCertificate()\n\n\ndef _validate_commit_certificate(",
        1,
    )
    probs = gate.frontier_problems(bad)
    assert any("construcción directa de CommitCertificate" in p for p in probs), (
        "el merge construyendo un cert debe fallar"
    )
