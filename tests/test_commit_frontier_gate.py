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
        "_validate_commit_certificate(certificate, expected_campaign=expected_campaign)  # forma/semántica/evidencia",
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


def test_gate_flags_mark_without_consume():
    # B234: mark_current_certified/mark_committed_incomplete deben CONSUMIR el cert (procedencia + uso unico).
    bad = _SRC.replace(
        "_consume_issued_certificate(certificate)  # B234: procedencia de la fábrica + consumo único (no replay/copia)",
        "pass  # sin consumir",
        1,
    )
    assert any("_consume_issued_certificate" in p for p in gate.frontier_problems(bad)), (
        "mark_* sin consumo debe fallar"
    )


def test_gate_flags_registry_mutated_outside_authorized():
    # B234: _ISSUED_CERTS solo se muta en _register_certificate/consume_commit_certificate.
    import pathlib

    import tools.campaign_bundle as cb

    cb_src = pathlib.Path(cb.__file__).read_text()
    bad = cb_src.replace(
        "def _validate_authority(camp_fd: int, bundle_id: str) -> None:",
        "def _evil():\n    _ISSUED_CERTS[123] = None\n\n\ndef _validate_authority(camp_fd: int, bundle_id: str) -> None:",
        1,
    )
    assert any("_ISSUED_CERTS mutado" in p for p in gate.factory_problems(bad)), "mutar el registro fuera debe fallar"


def test_gate_b237_flags_stray_authority_use(tmp_path, monkeypatch):
    # B237: un modulo de produccion FUERA de la fabrica/consumidor/gate que toque las primitivas de autoridad del
    # certificado (registro/consumo/registro-global/getattr/import/construccion) debe fallar el barrido de todo el arbol.
    stray = tmp_path / "stray.py"
    stray.write_text(
        "import tools.campaign_bundle as cb\n"
        "cb._register_certificate(x)\n"
        "cb.consume_commit_certificate(y)\n"
        "z = getattr(cb, '_ISSUED_CERTS')\n"
        "from tools.campaign_bundle import consume_commit_certificate\n"
        "c = cb.CommitCertificate(a=1)\n"
    )
    monkeypatch.setattr(gate, "_git_tracked_py", lambda: [str(stray)])
    probs = gate.authority_scope_problems()
    assert any("_register_certificate" in p for p in probs), "registro fuera de la fabrica debe fallar"
    assert any("consume_commit_certificate" in p for p in probs), "consumo fuera del consumidor debe fallar"
    assert any("_ISSUED_CERTS" in p for p in probs), "getattr del registro debe fallar"
    assert any("CONSTRUYE CommitCertificate" in p for p in probs), "construccion fuera de la fabrica debe fallar"


def test_gate_b237_fail_closed_on_git_failure(monkeypatch):
    # B237: si git ls-files no devuelve .py, es un problema (fail-closed), no un pase silencioso.
    monkeypatch.setattr(gate, "_git_tracked_py", list)
    assert gate.authority_scope_problems(), "git vacio debe fallar cerrado"


def test_gate_b242_const_concat_bypass_detected(tmp_path, monkeypatch):
    # B242: getattr(x, "_reg"+"ister_certificate") y __dict__["_ISSUED_"+"CERTS"] (concatenacion constante) se cazan.
    stray = tmp_path / "stray.py"
    stray.write_text(
        "import tools.campaign_bundle as cb\n"
        "getattr(cb, '_register_' + 'certificate')(x)\n"
        "cb.__dict__['_ISSUED_' + 'CERTS']\n"
        "getattr(cb, f'consume_{\"commit\"}_certificate')(y)\n"
    )
    monkeypatch.setattr(gate, "_git_tracked_py", lambda: [str(stray)])
    probs = gate.authority_scope_problems()
    assert any("_register_certificate" in p for p in probs), "concat en getattr debe fallar"
    assert any("_ISSUED_CERTS" in p for p in probs), "concat en __dict__ debe fallar"
    assert any("consume_commit_certificate" in p for p in probs), "f-string constante debe fallar"


def test_gate_b242_authority_modules_are_scanned(tmp_path, monkeypatch):
    # B242: los modulos de autoridad YA NO estan exentos por bloque — plantar 'registrar' o 'exec' en el consumidor
    # (o construir/consumir en el sitio equivocado) se caza por la allowlist POR OCURRENCIA.
    merge_like = tmp_path / "merge_like.py"
    merge_like.write_text("def x():\n    _register_certificate(c)\n    exec('y')\n    consume_commit_certificate(z)\n")
    monkeypatch.setattr(gate, "_git_tracked_py", lambda: [str(merge_like)])
    monkeypatch.setitem(
        gate._AUTHORITY_ALLOW,
        str(merge_like),
        {"consume_commit_certificate": frozenset({"_consume_issued_certificate"})},
    )
    probs = gate.authority_scope_problems()
    assert any("_register_certificate" in p for p in probs), "registrar en el consumidor debe fallar"
    assert any("exec()" in p for p in probs), "exec en un modulo de autoridad debe fallar"
    assert any("consume_commit_certificate" in p and "fn=x" in p for p in probs), (
        "consumir en fn no autorizada debe fallar"
    )


def test_gate_b242_real_code_clean():
    # el codigo real (fabrica+consumidor+resto) pasa la allowlist por-ocurrencia
    assert not gate.authority_scope_problems()


def test_gate_b245_dynamic_and_string_bypasses(tmp_path, monkeypatch):
    # B245: join/format/getattr-dinamico/__dict__/importlib/sys.modules/attrgetter sobre la superficie de autoridad
    # se cazan (resolucion de constantes + fail-closed ante acceso dinamico al modulo campaign_bundle).
    stray = tmp_path / "stray.py"
    stray.write_text(
        "import tools.campaign_bundle as cb\n"
        "import importlib, sys, operator\n"
        "getattr(cb, ''.join(['_reg', 'ister_certificate']))(x)\n"
        "getattr(cb, '_register_{}'.format('certificate'))(x)\n"
        "getattr(cb, some_var)(x)\n"
        "cb.__dict__['_ISSUED_CERTS']\n"
        "importlib.import_module('tools.campaign_bundle')\n"
        "sys.modules['tools.campaign_bundle']\n"
        "operator.attrgetter('consume_commit_certificate')(cb)\n"
    )
    monkeypatch.setattr(gate, "_git_tracked_py", lambda: [str(stray)])
    probs = gate.authority_scope_problems()
    assert any("_register_certificate" in p for p in probs), "join constante debe fallar"
    assert any("acceso dinámico" in p for p in probs), "getattr dinamico del modulo debe fallar"
    assert any("__dict__" in p for p in probs), "__dict__ debe fallar"
    assert any("import_module" in p for p in probs), "importlib de campaign_bundle debe fallar"
    assert any("sys.modules" in p for p in probs), "sys.modules debe fallar"
    assert any("consume_commit_certificate" in p for p in probs), "attrgetter constante debe fallar"


def test_gate_b248_requires_post_authority_reconcile():
    # B248: si commit_current pierde la reconciliacion post-autoridad (`if cert is not None ... _reconcile_and_raise`),
    # la fabrica-gate debe fallar.
    import pathlib

    import tools.campaign_bundle as cb

    cb_src = pathlib.Path(cb.__file__).read_text()
    bad = cb_src.replace("if cert is not None and primary is not None", "if False and primary is not None", 1)
    assert any("post-autoridad" in p or "B248" in p for p in gate.factory_problems(bad)), (
        "commit_current sin reconciliacion post-autoridad debe fallar"
    )
    assert not gate.factory_problems(cb_src), "el codigo real debe pasar"


def test_gate_b249_alias_propagation_and_dotted(tmp_path, monkeypatch):
    # B249: seguimiento de alias (mod = cb), cadenas y dotted import (tools.campaign_bundle) para cazar acceso dinamico.
    for name, src in {
        "alias_prop": "import tools.campaign_bundle as cb\nmod = cb\ngetattr(mod, name)(x)\n",
        "dotted": "import tools.campaign_bundle\ngetattr(tools.campaign_bundle, name)(x)\n",
        "chained": "import tools.campaign_bundle as cb\na=cb\nb=a\ngetattr(b, name)(x)\n",
        "vars": "import tools.campaign_bundle as cb\nvars(cb)\n",
        "dotted_dict": "import tools.campaign_bundle\ntools.campaign_bundle.__dict__[name]\n",
    }.items():
        f = tmp_path / f"{name}.py"
        f.write_text(src)
        monkeypatch.setattr(gate, "_git_tracked_py", lambda ff=f: [str(ff)])
        assert gate.authority_scope_problems(), f"evasion {name} debe fallar"
