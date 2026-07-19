"""Gate ESTRUCTURAL de la frontera de commit (P0R.5 · Incremento 2). La autoridad del commit es el CommitCertificate
de CURRENT; el recibo es evidencia. El gate lo enforce estáticamente sobre `tools/merge_campaign_pools.py`."""

from __future__ import annotations

import ast
import json
import os
import pathlib
import socket

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


def test_gate_b251_mandatory_post_authority_classify():
    # B251: la clasificacion post-autoridad debe ser UNA llamada obligatoria e INCONDICIONAL
    # `primary = _classify_post_authority(...)` de nivel de cuerpo, ANTES de quar.close(). Ningun decoy (if False,
    # llamada anidada, resultado descartado, orden invertido, funcion ausente) satisface el contrato.
    import pathlib

    import tools.campaign_bundle as cb

    cb_src = pathlib.Path(cb.__file__).read_text()
    call = "primary = _classify_post_authority(camp_fd, prepared, cert, primary)"
    assert call in cb_src, "fixture desincronizado: la llamada canonica cambio"
    # 1) decoy `cert and False` (anidada en un if muerto → no es nivel de cuerpo)
    dead = cb_src.replace(call, f"if cert is not None and False:\n        {call}", 1)
    assert gate.factory_problems(dead), "decoy `if cert and False` no debe satisfacer el contrato (B251)"
    # 2) resultado descartado (no reasignado a primary)
    discard = cb_src.replace(call, "_classify_post_authority(camp_fd, prepared, cert, primary)", 1)
    assert gate.factory_problems(discard), "llamada sin reasignar a primary debe fallar (B251)"
    # 3) llamada DESPUES de quar.close() (orden invertido)
    swapped = cb_src.replace(
        f"    {call}\n    close_errs = quar.close()",
        f"    close_errs = quar.close()\n    {call}",
        1,
    )
    assert swapped != cb_src and gate.factory_problems(swapped), "llamada tras quar.close() debe fallar (B251)"
    # 4) funcion obligatoria ausente
    gone = cb_src.replace("def _classify_post_authority(", "def _unused_classify_xx(", 1)
    assert gate.factory_problems(gone), "sin _classify_post_authority debe fallar (B251)"
    # 5) round 2: raise de nivel de cuerpo ANTES del classify -> lo deja inalcanzable (decoy)
    unreachable = cb_src.replace(f"    {call}", f"    raise primary\n    {call}", 1)
    assert any("inalcanzable" in p for p in gate.factory_problems(unreachable)), (
        "un raise antes del classify (inalcanzable) debe fallar (B251)"
    )
    # el codigo real pasa
    assert not gate.factory_problems(cb_src), "el codigo real debe pasar"


def test_classify_post_authority_reclassifies(monkeypatch):
    # B251 (runtime): _classify_post_authority reclasifica un fallo POSTERIOR al cert como CommittedStateError; y es
    # transparente cuando no hay cert (pre-CAS) o no hay fallo, y ante KeyboardInterrupt/SystemExit.
    import tools.campaign_bundle as cb

    class _Cert:
        previous_bundle_id = "prev"

    cert, err = _Cert(), RuntimeError("post-CAS boom")
    # reconciliacion: monkeypatchea _reconcile_and_raise para elevar CommittedStateError (el tipo taxonomico)
    monkeypatch.setattr(
        cb, "_reconcile_and_raise", lambda *a, **k: (_ for _ in ()).throw(cb.CommittedStateError("x", certificate=cert))
    )
    prepared = type("P", (), {"_ident": "id"})()
    out = cb._classify_post_authority(0, prepared, cert, err)
    assert isinstance(out, cb.CommittedStateError), "fallo post-cert → CommittedStateError"
    assert cb._classify_post_authority(0, prepared, None, err) is err, "sin cert (pre-CAS) → sin cambio"
    assert cb._classify_post_authority(0, prepared, cert, None) is None, "sin fallo → None"
    ki = KeyboardInterrupt()
    assert cb._classify_post_authority(0, prepared, cert, ki) is ki, "KeyboardInterrupt no se convierte"


def test_gate_b252_reflection_evasions(tmp_path, monkeypatch):
    # B252: alias por FIXPOINT (cadena larga), AnnAssign, walrus, alias de getattr/vars, attrgetter dinamico y
    # __import__ sobre la superficie de autoridad → todos fail-closed.
    # cadena de 20 alias en orden de documento INVERSO (a20=a19 … a1=cb): una sola pasada forward NO puede cascadear,
    # así que el `for _ in range(6)` viejo se queda corto y el fixpoint (while changed) sí converge.
    chain_lines = "\n".join(f"a{i} = a{i - 1}" for i in range(20, 0, -1)).replace("a1 = a0", "a1 = cb")
    chain = f"import tools.campaign_bundle as cb\n{chain_lines}\ngetattr(a20, name)(x)\n"
    cases = {
        "annassign": "import tools.campaign_bundle as cb\nmod: object = cb\ngetattr(mod, name)(x)\n",
        "walrus": "import tools.campaign_bundle as cb\n(mod := cb)\ngetattr(mod, name)(x)\n",
        "getattr_alias": "import tools.campaign_bundle as cb\ng = getattr\ng(cb, name)(x)\n",
        "vars_alias": "import tools.campaign_bundle as cb\nv = vars\nv(cb)[name]\n",
        "attrgetter_dyn": "import tools.campaign_bundle as cb\nimport operator\noperator.attrgetter(name)(cb)\n",
        "methodcaller_dyn": "import tools.campaign_bundle as cb\nimport operator\noperator.methodcaller(name)(cb)\n",
        "dunder_import": "import tools.campaign_bundle as cb\n__import__('tools.campaign_bundle')\n",
        "chain20": chain,
        # round 2: functools.partial capturando reflexion sobre cbref (nombre dinamico)
        "partial_form1": "import tools.campaign_bundle as cb\nimport functools\nfunctools.partial(getattr, cb)(name)\n",
        "partial_form2": "import tools.campaign_bundle as cb\nfrom functools import partial\npartial(getattr)(cb, name)\n",
        # round 2: attrgetter/methodcaller/getattr importados o encadenados bajo ALIAS
        "attrgetter_as": "import tools.campaign_bundle as cb\nfrom operator import attrgetter as ag\nag(name)(cb)\n",
        "methodcaller_as": "import tools.campaign_bundle as cb\nfrom operator import methodcaller as mc\nmc(name)(cb)\n",
        "getattr_from_builtins_as": "import tools.campaign_bundle as cb\nfrom builtins import getattr as g\ng(cb, name)\n",
    }
    for name, src in cases.items():
        f = tmp_path / f"{name}.py"
        f.write_text(src)
        monkeypatch.setattr(gate, "_git_tracked_py", lambda ff=f: [str(ff)])
        assert gate.authority_scope_problems(), f"evasion {name} debe fallar (B252)"


def test_gate_b252_no_false_positive_non_cb(tmp_path, monkeypatch):
    # B252 (fail-open control): reflexion dinamica sobre objetos NO-cb (self/certificate/otro modulo) NO debe fallar.
    f = tmp_path / "clean.py"
    f.write_text(
        "import tools.campaign_bundle as cb\n"
        "class C:\n"
        "    def m(self, campo):\n"
        "        return getattr(self, campo)\n"  # getattr sobre self, no sobre cb
        "def f(certificate, field):\n"
        "    return getattr(certificate, field)\n"  # sobre certificate, no cb
        "import operator\n"
        "operator.attrgetter('x')(some_other_obj)\n"  # attrgetter sobre otro objeto
        "from operator import attrgetter as ag\n"
        "ag('y')(some_other_obj)\n"  # attrgetter ALIAS sobre otro objeto
        "import functools\n"
        "functools.partial(getattr, some_other_obj)(k)\n"  # partial(getattr) sobre otro objeto
    )
    monkeypatch.setattr(gate, "_git_tracked_py", lambda: [str(f)])
    assert not gate.authority_scope_problems(), "reflexion sobre objetos no-cb no debe disparar (B252)"


def test_gate_b254_fingerprint_pins_critical_body(monkeypatch, tmp_path):
    # B254: el gate ya NO infiere alcanzabilidad con reglas parciales; PINEA el AST de las 3 funciones criticas
    # (commit_current/_classify_post_authority/_reconcile_and_raise) contra el contrato. Cualquier mutacion —incl. las
    # que el analizador viejo no veia: `if True: raise`, `while True`, return anidado, helper con return antes de
    # reconciliar, resultado descartado, funcion sustituta— cambia el fingerprint => el gate falla.
    import ast
    import json
    import pathlib

    assert gate.fingerprint_problems() == [], "el codigo real debe casar con el contrato de fingerprint"
    contract = json.loads((pathlib.Path(gate._ROOT) / gate._FINGERPRINT_CONTRACT).read_text())["functions"]
    src = pathlib.Path(gate._ROOT, "tools/campaign_bundle.py").read_text()
    call = "primary = _classify_post_authority(camp_fd, prepared, cert, primary)"
    reconcile_try = (
        "    try:\n        _reconcile_and_raise(camp_fd, prepared, prepared._ident, "
        'certificate.previous_bundle_id, primary, "post-authority")'
    )
    muts = {
        "nested_if_true_raise": src.replace(f"    {call}", f"    if True:\n        raise primary\n    {call}", 1),
        "while_true_before": src.replace(f"    {call}", f"    while True:\n        break\n    {call}", 1),
        "discarded_result": src.replace(call, "_classify_post_authority(camp_fd, prepared, cert, primary)", 1),
        "helper_early_return": src.replace(reconcile_try, f"    return primary\n{reconcile_try}", 1),
    }
    for label, msrc in muts.items():
        assert msrc != src, f"{label}: la mutacion no aplico (fixture desincronizado)"
        found = {n.name: n for n in ast.walk(ast.parse(msrc)) if isinstance(n, ast.FunctionDef)}
        changed = [name for name, want in contract.items() if gate._fn_fingerprint(found[name]) != want]
        assert changed, f"{label}: la mutacion NO cambio ningun fingerprint (B254)"
    # fail-closed: contrato ausente / funcion ausente
    monkeypatch.setattr(gate, "_FINGERPRINT_CONTRACT", "security/does_not_exist.json")
    assert gate.fingerprint_problems(), "contrato ausente debe fallar cerrado (B254)"


def test_gate_b254_wired_into_main():
    # B254: fingerprint_problems() esta cableado en main() (no es un chequeo huerfano).
    import inspect

    assert "fingerprint_problems" in inspect.getsource(gate.main), "fingerprint_problems debe correr en main()"


# Constantes LOCALES (no de `gate`) para que los tests corran igual en beab510 (RED via stash) y en el fix.
_CRIT = ("commit_current", "_classify_post_authority", "_reconcile_and_raise")
_ALGO = "sha256(ast.dump(top_level_FunctionDef,annotate_fields=True,include_attributes=False))"


def _run_fingerprint(monkeypatch, tmp_path, src, functions, *, raw_contract=None, **overrides):
    # monta un arbol sintetico (los 5 ficheros de autoridad + security/<contract> schema-3) y corre
    # fingerprint_problems(). El contrato base es VALIDO (schema-3 + authority_files) salvo lo que cada test override.
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "security").mkdir(exist_ok=True)
    (tmp_path / "tools" / "campaign_bundle.py").write_bytes(src.encode())
    afiles = {"tools/campaign_bundle.py": _sha256(src.encode())}
    for f in gate._AUTHORITY_FILES:
        if f != "tools/campaign_bundle.py":
            (tmp_path / f).write_bytes(b"# stub\n")
            afiles[f] = _sha256(b"# stub\n")
    if raw_contract is None:
        contract = {
            "schema_version": overrides.get("schema_version", 3),
            "note": "x",
            "source": overrides.get("source", "tools/campaign_bundle.py"),
            "algorithm": overrides.get("algorithm", _ALGO),
            "functions": functions,
            "authority_files_algorithm": gate._AUTHORITY_ALGORITHM,
            "authority_files": afiles,
        }
        raw = json.dumps(contract)
    else:
        raw = raw_contract
    (tmp_path / "security" / "commit_frontier_fingerprints.json").write_text(raw)
    for rel in (*gate._AUTHORITY_FILES, "security/commit_frontier_fingerprints.json"):
        (tmp_path / rel).chmod(0o644)  # B274: la lectura gobernada exige modo EXACTO 0644
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    return gate.fingerprint_problems(), ast.parse(src)


def _fp_of(tree, name, *, nested_in=None):
    if nested_in is not None:
        outer = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == nested_in)
        node = next(n for n in ast.walk(outer) if isinstance(n, ast.FunctionDef) and n.name == name and n is not outer)
    else:
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return gate._fn_fingerprint(node)


_SYN_OK = (
    "def commit_current(a, b):\n    return a + b\n\n"
    "def _classify_post_authority(a):\n    return a\n\n"
    "def _reconcile_and_raise(a):\n    raise a\n"
)


def test_b258_nested_homonym_cannot_mask_global_critical_function(monkeypatch, tmp_path):
    # B258: global commit_current MODIFICADO (raise) + decoy anidado con el AST aprobado. En beab510 el ast.walk elegia
    # el decoy y el gate quedaba verde; ahora el nivel-global modificado se caza (o el homonimo anidado se rechaza).
    src = (
        'def commit_current(a, b):\n    raise RuntimeError("bypass")\n\n'
        "def _classify_post_authority(a):\n    return a\n\n"
        "def _reconcile_and_raise(a):\n    raise a\n\n"
        "def decoy_container():\n    def commit_current(a, b):\n        return a + b\n    return commit_current\n"
    )
    tree = ast.parse(src)
    fns = {
        "commit_current": _fp_of(tree, "commit_current", nested_in="decoy_container"),  # hash del decoy aprobado
        "_classify_post_authority": _fp_of(tree, "_classify_post_authority"),
        "_reconcile_and_raise": _fp_of(tree, "_reconcile_and_raise"),
    }
    probs, _ = _run_fingerprint(monkeypatch, tmp_path, src, fns)
    assert probs, "un decoy anidado homonimo NO debe enmascarar el global critico modificado (B258)"


def test_b258_duplicate_top_level_critical_function_rejected(monkeypatch, tmp_path):
    src = _SYN_OK + "\ndef commit_current(a, b):\n    return a + b\n"  # segunda definicion GLOBAL
    tree = ast.parse(src)
    fns = {n: _fp_of(tree, n) for n in _CRIT}
    probs, _ = _run_fingerprint(monkeypatch, tmp_path, src, fns)
    assert any("exactamente 1" in p for p in probs), "dos definiciones globales de una funcion critica deben fallar (B258)"  # fmt: skip


def test_b258_contract_requires_exact_critical_function_set(monkeypatch, tmp_path):
    tree = ast.parse(_SYN_OK)
    full = {n: _fp_of(tree, n) for n in _CRIT}
    missing = {k: v for k, v in full.items() if k != "_reconcile_and_raise"}
    probs, _ = _run_fingerprint(monkeypatch, tmp_path, _SYN_OK, missing)
    assert any("functions" in p for p in probs), "un set de funciones incompleto debe fallar (B258)"
    extra = {**full, "extra_fn": "0" * 64}
    probs2, _ = _run_fingerprint(monkeypatch, tmp_path, _SYN_OK, extra)
    assert any("functions" in p for p in probs2), "una funcion extra en el contrato debe fallar (B258)"


def test_b258_contract_source_is_fixed(monkeypatch, tmp_path):
    tree = ast.parse(_SYN_OK)
    fns = {n: _fp_of(tree, n) for n in _CRIT}
    probs, _ = _run_fingerprint(monkeypatch, tmp_path, _SYN_OK, fns, source="tools/evil.py")
    assert any("source" in p for p in probs), "un source distinto de la constante debe fallar (B258)"
    probs2, _ = _run_fingerprint(monkeypatch, tmp_path, _SYN_OK, fns, algorithm="md5(x)")
    assert any("algorithm" in p for p in probs2), "un algorithm distinto de la constante debe fallar (B258)"


def test_b258_contract_rejects_duplicate_json_keys(monkeypatch, tmp_path):
    raw = (
        '{"schema_version": 2, "note": "x", "source": "tools/campaign_bundle.py", "source": "tools/evil.py",'
        ' "algorithm": "' + _ALGO + '", "functions": {}}'
    )
    probs, _ = _run_fingerprint(monkeypatch, tmp_path, _SYN_OK, {}, raw_contract=raw)
    assert any("duplicad" in p for p in probs), "claves JSON duplicadas deben fallar (B258)"


def test_b258_contract_schema_and_hash_types_are_exact(monkeypatch, tmp_path):
    tree = ast.parse(_SYN_OK)
    fns = {n: _fp_of(tree, n) for n in _CRIT}
    for bad_schema in (1, True, "2"):
        probs, _ = _run_fingerprint(monkeypatch, tmp_path, _SYN_OK, fns, schema_version=bad_schema)
        assert any("schema_version" in p for p in probs), f"schema_version={bad_schema!r} debe fallar (B258)"
    bad_hash = {**fns, "commit_current": "not-64-hex"}
    probs2, _ = _run_fingerprint(monkeypatch, tmp_path, _SYN_OK, bad_hash)
    assert any("hash" in p or "64" in p for p in probs2), "un hash no [0-9a-f]{64} debe fallar (B258)"


def _run_rebind(monkeypatch, tmp_path, extra_src):
    # el AST APROBADO de las 3 funciones se preserva; el fallo proviene del BINDING, no del fingerprint.
    src = _SYN_OK + extra_src
    tree = ast.parse(src)
    defs, _ = _critical_defs(tree)
    fns = {n: _fp_of(tree, n) for n in _CRIT} if len(defs) == 3 else {n: "0" * 64 for n in _CRIT}
    probs, _ = _run_fingerprint(monkeypatch, tmp_path, src, fns)
    return probs


def _critical_defs(tree):
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in _CRIT}, None


def test_b264_top_level_assign_cannot_replace_critical_binding(monkeypatch, tmp_path):
    assert _run_rebind(monkeypatch, tmp_path, "\ncommit_current = lambda *a, **k: None\n"), "rebind por Assign debe fallar (B264)"  # fmt: skip


def test_b264_annotated_and_unpack_bindings_rejected(monkeypatch, tmp_path):
    assert _run_rebind(monkeypatch, tmp_path, "\ncommit_current: object = None\n"), "AnnAssign debe fallar (B264)"
    assert _run_rebind(monkeypatch, tmp_path, "\ncommit_current, x = None, 1\n"), "unpack debe fallar (B264)"
    assert _run_rebind(monkeypatch, tmp_path, "\n(commit_current := None)\n"), (
        "walrus a nivel módulo debe fallar (B264)"
    )


def test_b264_import_alias_and_star_import_rejected(monkeypatch, tmp_path):
    assert _run_rebind(monkeypatch, tmp_path, "\nfrom os import getcwd as commit_current\n"), "import-as debe fallar (B264)"  # fmt: skip
    assert _run_rebind(monkeypatch, tmp_path, "\nfrom os import *\n"), "import * debe fallar (B264)"
    assert _run_rebind(monkeypatch, tmp_path, "\nclass commit_current:\n    pass\n"), "class-rebind debe fallar (B264)"


def test_b264_del_and_control_flow_targets_rejected(monkeypatch, tmp_path):
    assert _run_rebind(monkeypatch, tmp_path, "\ndel commit_current\n"), "del debe fallar (B264)"
    assert _run_rebind(monkeypatch, tmp_path, "\nfor commit_current in []:\n    pass\n"), (
        "for-target debe fallar (B264)"
    )
    assert _run_rebind(monkeypatch, tmp_path, "\nimport contextlib\nwith contextlib.nullcontext() as commit_current:\n    pass\n"), "with-target debe fallar (B264)"  # fmt: skip


def test_b264_global_declaration_and_nested_write_rejected(monkeypatch, tmp_path):
    assert _run_rebind(monkeypatch, tmp_path, "\ndef evil():\n    global commit_current\n    commit_current = None\n"), "global+write debe fallar (B264)"  # fmt: skip


def test_b264_dynamic_module_binding_mutation_rejected(monkeypatch, tmp_path):
    assert _run_rebind(monkeypatch, tmp_path, "\nglobals()['commit_current'] = None\n"), "globals()[...] debe fallar (B264)"  # fmt: skip
    assert _run_rebind(monkeypatch, tmp_path, "\nimport sys\nsetattr(sys.modules[__name__], 'commit_current', None)\n"), "setattr(module) debe fallar (B264)"  # fmt: skip
    assert _run_rebind(monkeypatch, tmp_path, "\nexec('commit_current = None')\n"), "exec debe fallar (B264)"


def test_b264_harmless_local_same_name_without_global_is_allowed(monkeypatch, tmp_path):
    # una variable LOCAL homónima (sin `global`) en una función no crítica NO re-liga el binding del módulo.
    assert not _run_rebind(monkeypatch, tmp_path, "\ndef unrelated():\n    commit_current = 1\n    return commit_current\n"), "local homónimo no debe fallar (B264)"  # fmt: skip


def _run_authority(monkeypatch, tmp_path, *, tamper_bytes=b""):
    # monta un árbol sintético con los 5 ficheros de autoridad + contrato schema-3; el contrato se computa sobre los
    # bytes LIMPIOS y luego se AÑADE tamper_bytes a campaign_bundle → sus bytes reales difieren del hash pineado, aunque
    # el AST del `def` NO cambie (B269: mutación del objeto función / __code__ / alias / callback import-time).
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "security").mkdir(exist_ok=True)
    cb = _SYN_OK.encode()
    others = {f: b"# stub authority module\n" for f in gate._AUTHORITY_FILES if f != "tools/campaign_bundle.py"}
    tree = ast.parse(_SYN_OK)
    fns = {n: _fp_of(tree, n) for n in _CRIT}
    afiles = {"tools/campaign_bundle.py": _sha256(cb), **{f: _sha256(b) for f, b in others.items()}}
    contract = {
        "schema_version": 3, "note": "x", "source": "tools/campaign_bundle.py", "algorithm": _ALGO,
        "functions": fns, "authority_files_algorithm": gate._AUTHORITY_ALGORITHM, "authority_files": afiles,
    }  # fmt: skip
    (tmp_path / "tools" / "campaign_bundle.py").write_bytes(cb + tamper_bytes)
    for f, b in others.items():
        (tmp_path / f).write_bytes(b)
    (tmp_path / "security" / "commit_frontier_fingerprints.json").write_text(json.dumps(contract))
    for rel in (*gate._AUTHORITY_FILES, "security/commit_frontier_fingerprints.json"):
        (tmp_path / rel).chmod(0o644)  # B274: la lectura gobernada exige modo EXACTO 0644
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    return gate.fingerprint_problems()


def _sha256(b: bytes) -> str:
    import hashlib

    return hashlib.sha256(b).hexdigest()


def test_b269_function_code_mutation_changes_authority_blob(monkeypatch, tmp_path):
    # el AST de los 3 defs NO cambia, pero un `commit_current.__code__ = evil.__code__` altera los BYTES del fichero.
    assert not _run_authority(monkeypatch, tmp_path), "el árbol limpio debe pasar"
    probs = _run_authority(monkeypatch, tmp_path, tamper_bytes=b"\ndef evil(*a, **k):\n    return None\ncommit_current.__code__ = evil.__code__\n")  # fmt: skip
    assert any("bytes cambiaron" in p or "B269" in p for p in probs), "una mutación de __code__ debe romper la autoridad (B269)"  # fmt: skip


def test_b269_alias_and_defaults_change_authority_blob(monkeypatch, tmp_path):
    for tamper in (b"\n_alias = commit_current\n", b"\ncommit_current.__defaults__ = ()\n"):
        assert any("B269" in p or "bytes cambiaron" in p for p in _run_authority(monkeypatch, tmp_path, tamper_bytes=tamper)), f"{tamper!r} debe romper la autoridad (B269)"  # fmt: skip


def test_b269_import_time_callback_changes_authority_blob(monkeypatch, tmp_path):
    probs = _run_authority(monkeypatch, tmp_path, tamper_bytes=b"\ndef _on_import():\n    pass\n_on_import()\n")
    assert any("B269" in p or "bytes cambiaron" in p for p in probs), "un callback import-time debe romper la autoridad (B269)"  # fmt: skip


def test_b269_contract_schema_and_files_exact():
    # el contrato REAL pasa; hashes/rutas mal → fail-closed.
    import json as _json
    import pathlib

    real = _json.loads((pathlib.Path(gate._ROOT) / gate._FINGERPRINT_CONTRACT).read_text())
    assert gate._authority_files_problems(real) == [], "el contrato real debe validar los bytes de autoridad"
    bad = _json.loads(_json.dumps(real))
    bad["authority_files"]["tools/governed_fs.py"] = "0" * 64
    assert any("governed_fs" in p for p in gate._authority_files_problems(bad)), "hash gobernado a ceros debe fallar"
    miss = _json.loads(_json.dumps(real))
    miss["authority_files"].pop("tools/atomic_fs.py")
    assert gate._fingerprint_contract_problems(miss), "ruta de autoridad faltante debe fallar (B269)"
    old = {k: v for k, v in real.items() if k not in ("authority_files", "authority_files_algorithm")}
    old["schema_version"] = 2
    assert gate._fingerprint_contract_problems(old), "un contrato schema-2 sin authority_files debe fallar (B269)"


# ---------------------------------------------------------------------------
# B274 — lectura GOBERNADA del contrato y de los módulos de autoridad.
# RED_BASE_SHA = 036c8f9 (el `_read_regular_nofollow` sólo protegía el leaf: sin O_NOFOLLOW por componente, sin exigir
# uid/nlink/modo/no-especiales, sin O_NONBLOCK, sin snapshot pre/post, reabriendo el contrato/crítico por ruta).
# expected_old_behavior: aceptar (rc0 / sin problema) un árbol con ancestro symlink, modo laxo, hardlink u objeto especial.
# ---------------------------------------------------------------------------
_GOVERN_RELS = (*getattr(gate, "_AUTHORITY_FILES", ()), "security/commit_frontier_fingerprints.json")


def _lay_governed_tree(tmp_path):
    """Árbol sintético VÁLIDO: 5 ficheros de autoridad (campaign_bundle = _SYN_OK) + contrato schema-3, todos 0644."""
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "security").mkdir(exist_ok=True)
    cb = _SYN_OK.encode()
    (tmp_path / "tools" / "campaign_bundle.py").write_bytes(cb)
    afiles = {"tools/campaign_bundle.py": _sha256(cb)}
    for f in gate._AUTHORITY_FILES:
        if f != "tools/campaign_bundle.py":
            (tmp_path / f).write_bytes(b"# stub\n")
            afiles[f] = _sha256(b"# stub\n")
    tree = ast.parse(_SYN_OK)
    contract = {
        "schema_version": 3, "note": "x", "source": "tools/campaign_bundle.py", "algorithm": _ALGO,
        "functions": {n: _fp_of(tree, n) for n in _CRIT},
        "authority_files_algorithm": gate._AUTHORITY_ALGORITHM, "authority_files": afiles,
    }  # fmt: skip
    (tmp_path / "security" / "commit_frontier_fingerprints.json").write_text(json.dumps(contract))
    for rel in _GOVERN_RELS:
        (tmp_path / rel).chmod(0o644)
    return tmp_path


def _govern(monkeypatch, tmp_path, rel):
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    return gate._read_governed_repo_file(rel)


def test_b274_happy_control_accepts_regular_0644(monkeypatch, tmp_path):
    _lay_governed_tree(tmp_path)
    for rel in _GOVERN_RELS:
        gb, probs = _govern(monkeypatch, tmp_path, rel)
        assert gb is not None and probs == [], f"{rel} 0644/uid/nlink1 debe aceptarse (B274): {probs}"
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    assert gate.fingerprint_problems() == [], "el árbol gobernado limpio debe pasar el fingerprint (B274)"


def test_b274_invalid_rel_paths_rejected(monkeypatch, tmp_path):
    _lay_governed_tree(tmp_path)
    for rel in ("/etc/passwd", "tools/../security/x.json", "tools//campaign_bundle.py", "", "tools/./x", "a\x00b"):
        gb, probs = _govern(monkeypatch, tmp_path, rel)
        assert gb is None and probs, f"rel inválida {rel!r} debe rechazarse (B274)"


def test_b274_ancestor_symlink_rejected(monkeypatch, tmp_path):
    # `tools` y `security` como symlink a otro árbol con bytes aprobados → la cadena O_NOFOLLOW por componente lo corta.
    _lay_governed_tree(tmp_path)
    shadow = tmp_path.parent / (tmp_path.name + "_shadow")
    (shadow / "tools").mkdir(parents=True)
    (shadow / "security").mkdir(parents=True)
    (shadow / "tools" / "campaign_bundle.py").write_bytes(_SYN_OK.encode())
    (shadow / "tools" / "campaign_bundle.py").chmod(0o644)
    for comp, rel in (
        ("tools", "tools/campaign_bundle.py"),
        ("security", "security/commit_frontier_fingerprints.json"),
    ):
        victim = tmp_path / comp
        backup = tmp_path / (comp + "_real")
        victim.rename(backup)
        victim.symlink_to(shadow / comp)
        try:
            gb, probs = _govern(monkeypatch, tmp_path, rel)
            assert gb is None and any("B274" in p for p in probs), f"ancestro symlink {comp} debe rechazarse (B274)"
        finally:
            victim.unlink()
            backup.rename(victim)


def test_b274_leaf_symlink_and_broken_rejected(monkeypatch, tmp_path):
    _lay_governed_tree(tmp_path)
    for rel in ("tools/campaign_bundle.py", "security/commit_frontier_fingerprints.json"):
        leaf = tmp_path / rel
        real = leaf.with_suffix(leaf.suffix + ".real")
        leaf.rename(real)
        leaf.symlink_to(real)  # symlink a fichero real aprobado
        gb, probs = _govern(monkeypatch, tmp_path, rel)
        assert gb is None and any("B274" in p for p in probs), f"leaf symlink {rel} debe rechazarse (B274)"
        leaf.unlink()
        leaf.symlink_to(tmp_path / "does_not_exist")  # symlink roto
        gb, probs = _govern(monkeypatch, tmp_path, rel)
        assert gb is None and any("B274" in p for p in probs), f"leaf symlink roto {rel} debe rechazarse (B274)"
        leaf.unlink()
        real.rename(leaf)


def test_b274_lax_modes_rejected(monkeypatch, tmp_path):
    for mode in (0o666, 0o664, 0o600, 0o755):
        for rel in ("tools/campaign_bundle.py", "security/commit_frontier_fingerprints.json"):
            _lay_governed_tree(tmp_path)
            (tmp_path / rel).chmod(mode)
            gb, probs = _govern(monkeypatch, tmp_path, rel)
            assert gb is None and any("modo" in p and "B274" in p for p in probs), f"modo {oct(mode)} {rel} debe rechazarse (B274)"  # fmt: skip


def test_b274_hardlink_rejected(monkeypatch, tmp_path):
    for rel in ("tools/campaign_bundle.py", "security/commit_frontier_fingerprints.json"):
        _lay_governed_tree(tmp_path)
        os.link(tmp_path / rel, tmp_path / (rel + ".hard"))  # nlink pasa a 2
        gb, probs = _govern(monkeypatch, tmp_path, rel)
        assert gb is None and any("nlink" in p and "B274" in p for p in probs), f"hardlink {rel} debe rechazarse (B274)"


def test_b274_fifo_leaf_does_not_hang(monkeypatch, tmp_path):
    # FIFO sin escritor: O_NONBLOCK evita el cuelgue. Se envuelve la lectura en un temporizador REAL killable (SIGALRM,
    # 2 s): si faltara O_NONBLOCK, el `os.open` bloqueante sería interrumpido (PEP 475) y el test FALLA, no cuelga.
    import signal

    def _on_timeout(signum, frame):
        raise TimeoutError("la lectura gobernada colgó en un FIFO (falta O_NONBLOCK) (B274)")

    for rel in ("tools/campaign_bundle.py", "security/commit_frontier_fingerprints.json"):
        _lay_governed_tree(tmp_path)
        (tmp_path / rel).unlink()
        os.mkfifo(tmp_path / rel)
        old = signal.signal(signal.SIGALRM, _on_timeout)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            gb, probs = _govern(monkeypatch, tmp_path, rel)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            (tmp_path / rel).unlink()  # quitar el FIFO: un `write_bytes` posterior sobre él bloquearía sin lector
        assert gb is None and probs, f"FIFO {rel} debe rechazarse sin colgar (B274)"


def test_b274_socket_leaf_rejected(monkeypatch):
    import shutil
    import tempfile

    short = tempfile.mkdtemp(
        prefix="b", dir="/tmp"
    )  # AF_UNIX exige ruta corta (macOS ~104 chars); pytest tmp_path no cabe
    os.mkdir(os.path.join(short, "tools"))
    rel = "tools/campaign_bundle.py"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(os.path.join(short, rel))
        monkeypatch.setattr(gate, "_ROOT", short)
        monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
        gb, probs = gate._read_governed_repo_file(rel)
        assert gb is None and probs, "un socket Unix como leaf debe rechazarse (B274)"
    finally:
        srv.close()
        shutil.rmtree(short, ignore_errors=True)


def test_b274_leaf_inode_swapped_during_read_rejected(monkeypatch, tmp_path):
    # Reemplazo del leaf entre snapshot inicial/final: el fd retiene el inode viejo pero el NOMBRE resuelve a otro → la
    # revalidación nombre↔inode lo caza. Se inyecta el swap justo tras el primer fstat, vía monkeypatch de os.read.
    _lay_governed_tree(tmp_path)
    rel = "tools/campaign_bundle.py"
    leaf = tmp_path / rel
    real_read = os.read
    swapped = {"done": False}

    def _read_then_swap(fd, n):
        if not swapped["done"]:
            swapped["done"] = True
            leaf.unlink()
            leaf.write_bytes(_SYN_OK.encode() + b"\n# other inode\n")
            leaf.chmod(0o644)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", _read_then_swap)
    gb, probs = _govern(monkeypatch, tmp_path, rel)
    assert gb is None and any("B274" in p for p in probs), "un swap de leaf durante la lectura debe rechazarse (B274)"


def test_b274_read_and_close_errors_fail_closed(monkeypatch, tmp_path):
    _lay_governed_tree(tmp_path)
    rel = "tools/campaign_bundle.py"
    real_read = os.read

    def _boom_read(fd, n):
        raise OSError(5, "EIO inyectado")

    monkeypatch.setattr(os, "read", _boom_read)
    gb, probs = _govern(monkeypatch, tmp_path, rel)
    assert gb is None and any("lectura" in p and "B274" in p for p in probs), (
        "error de read debe ser fail-closed (B274)"
    )
    monkeypatch.setattr(os, "read", real_read)

    real_close = os.close
    tripped = {"n": 0}

    def _boom_close(fd):
        # falla SÓLO el primer close (el del leaf); los cierres de dir_fds del finally siguen reales.
        if tripped["n"] == 0:
            tripped["n"] = 1
            try:
                real_close(fd)
            finally:
                raise OSError(9, "EBADF inyectado")
        return real_close(fd)

    monkeypatch.setattr(os, "close", _boom_close)
    gb, probs = _govern(monkeypatch, tmp_path, rel)
    assert gb is None and any("cerrar" in p and "B274" in p for p in probs), "un cierre fallido debe invalidar el resultado (B274)"  # fmt: skip


# --- RED-first conductual de B274 a través del API público estable `fingerprint_problems()` (corre en BASE y HEAD).
# En 036c8f9 el gate lee por ruta ignorando modo/nlink/ancestro-symlink → ACEPTA (rc0) árboles comprometidos cuyos
# BYTES siguen coincidiendo con el contrato. Estas tres pruebas FALLAN en 036c8f9 (esperan un problema y no lo hay) y
# pasan aquí. No dependen de `_read_governed_repo_file` (API nueva), así que son RED conductuales legítimas.
def test_b274_behavioral_lax_mode_authority_rejected_now(monkeypatch, tmp_path):
    _lay_governed_tree(tmp_path)
    (tmp_path / "tools" / "governed_fs.py").chmod(0o666)  # world-writable: los bytes no cambian, el modo sí
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    assert any("B274" in p for p in gate.fingerprint_problems()), "un fichero de autoridad 0666 debe rechazarse (B274)"


def test_b274_behavioral_hardlink_authority_rejected_now(monkeypatch, tmp_path):
    _lay_governed_tree(tmp_path)
    os.link(tmp_path / "tools" / "governed_fs.py", tmp_path / "tools" / "governed_fs_hard.py")  # nlink→2
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    assert any("B274" in p for p in gate.fingerprint_problems()), "un fichero de autoridad con hardlink debe rechazarse (B274)"  # fmt: skip


def test_b274_behavioral_ancestor_symlink_rejected_now(monkeypatch, tmp_path):
    import shutil

    _lay_governed_tree(tmp_path)
    shadow = tmp_path.parent / (tmp_path.name + "_shadow")
    (shadow / "tools").mkdir(parents=True)
    for f in (
        gate._AUTHORITY_FILES
    ):  # el shadow lleva los MISMOS bytes aprobados → el gate viejo (que sigue el symlink) acepta
        shutil.copy(tmp_path / f, shadow / f)
        (shadow / f).chmod(0o644)
    (tmp_path / "tools").rename(tmp_path / "tools_real")
    (tmp_path / "tools").symlink_to(shadow / "tools")
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    assert any("B274" in p for p in gate.fingerprint_problems()), "tools/ como symlink debe rechazarse aunque los bytes coincidan (B274)"  # fmt: skip


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


def test_gate_b249_taint_obfuscation(tmp_path, monkeypatch):
    # B249 (fail-closed): un destino asignado desde CUALQUIER expresion que CONTENGA una ref al modulo (list-index,
    # tuple-unpack, dict-value) se trata como posible alias -> getattr dinamico sobre el se caza.
    for name, src in {
        "list_index": "import tools.campaign_bundle as cb\nx = [cb][0]\ngetattr(x, name)(z)\n",
        "tuple_unpack": "import tools.campaign_bundle as cb\na, b = cb, 1\ngetattr(a, name)(z)\n",
        "dict_value": "import tools.campaign_bundle as cb\nx = {'m': cb}['m']\ngetattr(x, name)(z)\n",
    }.items():
        f = tmp_path / f"{name}.py"
        f.write_text(src)
        monkeypatch.setattr(gate, "_git_tracked_py", lambda ff=f: [str(ff)])
        assert gate.authority_scope_problems(), f"ofuscacion {name} debe fallar (fail-closed)"


def test_gate_b249_real_code_clean():
    assert not gate.authority_scope_problems()  # el arbol real no dispara falsos positivos del taint


# ---------------------------------------------------------------------------
# B279 — evidencia RED CONDUCTUAL (no schema-absence) para B269. `_adaptive_fingerprint` construye el contrato que el
# módulo de ESTA era ENTIENDE (schema-2 sin authority_files en 2ce76d8; schema-3 con authority_files aquí) usando sus
# propias constantes, así `fingerprint_problems()` corre DE VERDAD en ambos SHAs. El payload muta el OBJETO función
# (`commit_current.__code__ = _evil.__code__`) sin tocar el AST de los tres `def`: en 2ce76d8 (sólo AST) se ACEPTA
# (RED), aquí el hash del fichero completo lo RECHAZA. Fe de erratas: el RED previo (test schema-3 en 2ce76d8) sólo
# probaba ausencia de maquinaria, no la conducta vulnerable.
# ---------------------------------------------------------------------------
_B279_TAMPER = b"\ndef _evil(a, b):\n    return 999\ncommit_current.__code__ = _evil.__code__\n"


def _adaptive_fingerprint(tmp_path, monkeypatch, tamper=b""):
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "security").mkdir(exist_ok=True)
    clean = _SYN_OK.encode()
    (tmp_path / "tools" / "campaign_bundle.py").write_bytes(clean + tamper)  # el FICHERO lleva el tamper
    fns = {n: _fp_of(ast.parse(_SYN_OK), n) for n in _CRIT}  # fingerprints de los defs LIMPIOS (== defs con tamper)
    has_auth = hasattr(gate, "_AUTHORITY_FILES")
    contract = {
        "schema_version": gate._FINGERPRINT_SCHEMA, "note": "x", "source": "tools/campaign_bundle.py",
        "algorithm": _ALGO, "functions": fns,
    }  # fmt: skip
    chmod_rels = ["tools/campaign_bundle.py", "security/commit_frontier_fingerprints.json"]
    if has_auth:
        afiles = {"tools/campaign_bundle.py": _sha256(clean)}  # APROBADO sobre los bytes LIMPIOS
        for f in gate._AUTHORITY_FILES:
            if f != "tools/campaign_bundle.py":
                (tmp_path / f).write_bytes(b"# stub\n")
                afiles[f] = _sha256(b"# stub\n")
                chmod_rels.append(f)
        contract["authority_files_algorithm"] = gate._AUTHORITY_ALGORITHM
        contract["authority_files"] = afiles
    (tmp_path / "security" / "commit_frontier_fingerprints.json").write_text(json.dumps(contract))
    for rel in chmod_rels:
        (tmp_path / rel).chmod(0o644)
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_tracked", lambda r: True)
    return gate.fingerprint_problems()


def test_b279_b269_code_object_mutation_behavioral(monkeypatch, tmp_path):
    # probe de runtime: el payload cambia DE VERDAD el comportamiento de commit_current (de a+b a 999).
    ns: dict = {}
    exec(_SYN_OK + _B279_TAMPER.decode(), ns)  # noqa: S102  (payload de prueba controlado)
    assert ns["commit_current"](1, 2) == 999, "el payload __code__ debe cambiar el comportamiento (B279/B269)"
    # árbol limpio: aceptado en ambas eras (control)
    assert _adaptive_fingerprint(tmp_path, monkeypatch) == [], "el árbol limpio debe pasar (B279)"
    # con el tamper: el AST de los 3 defs NO cambia; sólo cambian los BYTES del fichero.
    probs = _adaptive_fingerprint(tmp_path, monkeypatch, _B279_TAMPER)
    assert probs, "la mutación del objeto función (bytes del fichero) debe rechazarse (B279/B269)"
