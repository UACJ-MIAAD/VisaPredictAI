"""B255/B259/B260: gate de reflexión por REGISTRO POSITIVO con IDENTIDAD SEMÁNTICA (tools/check_reflection.py). Toda
reflexión de producción está declarada por un ID derivado de {file, qualname, op, statement_ast_sha256,
occurrence_index}; un cambio de objeto/nombre/forma de la llamada cambia el ID; el scanner es fail-closed ante
sintaxis/lectura/JSON inválidos y cubre builtins/sys/importlib/operator/functools por alias, imports relativos, async,
métodos y funciones anidadas."""

from __future__ import annotations

import json

import tools.check_reflection as refl


def _mount(tmp_path, monkeypatch, files: dict[str, str]):
    """Escribe `files` (rel→contenido) bajo tmp_path y apunta refl.ROOT + refl._production_files ahí."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    monkeypatch.setattr(refl, "ROOT", str(tmp_path))
    monkeypatch.setattr(refl, "_production_files", lambda: list(files))


def test_reflection_registry_is_satisfied():
    assert refl.problems() == []


def test_b259_semantic_change_changes_occurrence_id(tmp_path, monkeypatch):
    # B259: mismo file/qualname/op pero distinto objeto/argumento → distinto statement_ast → distinto ID.
    _mount(tmp_path, monkeypatch, {"m.py": "def g(x, y, dyn):\n    return getattr(x, 'old')\n"})
    id_old = next(iter(refl.scan_reflection(["m.py"])[0]))
    _mount(tmp_path, monkeypatch, {"m.py": "def g(x, y, dyn):\n    return getattr(y, dyn)\n"})
    id_new = next(iter(refl.scan_reflection(["m.py"])[0]))
    assert id_old != id_new, "un cambio de objeto/argumento debe cambiar el occurrence ID (B259)"


def test_b259_registered_occurrence_change_requires_review(tmp_path, monkeypatch):
    # una ocurrencia registrada que cambia de semantica queda NO REGISTRADA (nuevo ID) → el gate falla.
    src_old = "def g(x, dyn):\n    return getattr(x, 'STATIC')\n"
    _mount(tmp_path, monkeypatch, {"m.py": src_old})
    entries0, _ = refl.scan_reflection(["m.py"])
    eid, occ = next(iter(entries0.items()))
    reg = {
        "schema_version": refl._SCHEMA_VERSION,
        "scanner_version": refl._SCANNER_VERSION,
        "note": "x",
        "operations_controlled": list(refl.OPERATIONS_CONTROLLED),
        "authorized_campaign_bundle_importers": [],
        "entries": {
            eid: {k: occ[k] for k in ("file", "qualname", "op", "statement_ast_sha256", "occurrence_index")}
            | {"justification": "j", "review_by": "2027-07-31"}
        },
    }
    (tmp_path / refl._REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / refl._REGISTRY).write_text(json.dumps(reg))
    assert refl.problems() == [], "el registro que coincide debe pasar"
    _mount(tmp_path, monkeypatch, {"m.py": "def g(x, dyn):\n    return getattr(x, dyn)\n"})
    (tmp_path / refl._REGISTRY).write_text(json.dumps(reg))
    assert any("NO REGISTRADA" in p or "OBSOLETA" in p for p in refl.problems()), "cambio semantico debe fallar (B259)"


def test_b260_invalid_syntax_is_fail_closed(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"m.py": "def f(:\n  pass\n"})
    _, probs = refl.scan_reflection(["m.py"])
    assert any("SyntaxError" in p for p in probs), "sintaxis invalida debe dar problema, no continue (B260)"


def test_b260_unreadable_file_is_fail_closed(monkeypatch):
    monkeypatch.setattr(refl, "ROOT", "/nonexistent_root_xyz")
    monkeypatch.setattr(refl, "_production_files", lambda: ["m.py"])
    _, probs = refl.scan_reflection(["m.py"])
    assert any("ilegible" in p for p in probs), "fichero ilegible debe dar problema (B260)"


def test_b260_qualified_builtins_and_module_aliases(tmp_path, monkeypatch):
    cases = {
        "builtins_getattr": "import builtins\nbuiltins.getattr(o, n)\n",
        "builtins_setattr": "import builtins\nbuiltins.setattr(o, n, v)\n",
        "builtins_dunder_import": "import builtins\nbuiltins.__import__(n)\n",
        "builtins_eval": "import builtins\nbuiltins.eval(s)\n",
        "builtins_alias": "import builtins as b\nb.getattr(o, n)\n",
        "sys_alias": "import sys as s\ns.modules[n]\n",
        "importlib_alias": "import importlib as il\nil.import_module(n)\n",
        "operator_alias": "import operator as op\nop.attrgetter('x')(o)\n",
        # ronda B: __builtins__ (módulo/dict) + alias de módulo encadenado
        "dunder_builtins_attr": "__builtins__.getattr(o, n)\n",
        "dunder_builtins_subscript": "__builtins__['getattr'](o, n)\n",
        "chained_module_alias": "import builtins\nb2 = builtins\nb2.getattr(o, n)\n",
        "double_chained_alias": "import builtins\nb2 = builtins\nb3 = b2\nb3.setattr(o, n, v)\n",
    }
    for label, src in cases.items():
        _mount(tmp_path, monkeypatch, {"m.py": src})
        ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
        assert ops, f"{label} debe detectarse (B260): {ops}"


def test_b260_no_false_positive_non_builtins_subscript(tmp_path, monkeypatch):
    # control: un subscript sobre un dict cualquiera (no __builtins__) NO debe detectarse como reflexión.
    _mount(tmp_path, monkeypatch, {"m.py": "d = {}\nd['getattr']\nq = obj.something\n"})
    assert refl.scan_reflection(["m.py"])[0] == {}, "subscript no-builtins no debe disparar (B260)"


def test_b260_from_import_aliases_and_multi(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"m.py": "from builtins import getattr as g\ng(x, n)\n"})
    assert {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()} == {"getattr"}
    # dos primitivos aliased en un solo ImportFrom → ambas ops
    _mount(tmp_path, monkeypatch, {"m.py": "from operator import attrgetter as ag, methodcaller as mc\nag('x')(o)\nmc('y')(o)\n"})  # fmt: skip
    ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert {"attrgetter", "methodcaller"} <= ops, f"ImportFrom multi-op debe dar ambas ops (B260): {ops}"


def test_b260_async_and_nested_qualname(tmp_path, monkeypatch):
    src = (
        "class C:\n"
        "    async def m(self, o, n):\n"
        "        return getattr(o, n)\n"
        "    def outer(self, o, n):\n"
        "        def inner():\n"
        "            return getattr(o, n)\n"
        "        return inner\n"
    )
    _mount(tmp_path, monkeypatch, {"m.py": src})
    quals = {e["qualname"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert "C.m" in quals, f"async method qualname debe capturarse (B260): {quals}"
    assert any(q.startswith("C.outer.inner") for q in quals), f"nested qualname debe capturarse: {quals}"


def test_b260_relative_cb_importers(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"tools/x.py": "from .campaign_bundle import commit_current\n"})
    imp, _ = refl.scan_cb_importers(["tools/x.py"])
    assert "tools/x.py" in imp, "importador relativo `from .campaign_bundle` debe detectarse (B260)"
    # y falla el gate como importador no autorizado
    reg = {
        "schema_version": refl._SCHEMA_VERSION, "scanner_version": refl._SCANNER_VERSION, "note": "x",
        "operations_controlled": list(refl.OPERATIONS_CONTROLLED), "authorized_campaign_bundle_importers": [],
        "entries": {},
    }  # fmt: skip
    (tmp_path / refl._REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / refl._REGISTRY).write_text(json.dumps(reg))
    assert any("IMPORTADOR NO AUTORIZADO" in p for p in refl.problems())


def test_b260_registry_schema_and_metadata_fail_closed(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"m.py": "x = 1\n"})
    base = {
        "schema_version": refl._SCHEMA_VERSION, "scanner_version": refl._SCANNER_VERSION, "note": "x",
        "operations_controlled": list(refl.OPERATIONS_CONTROLLED), "authorized_campaign_bundle_importers": [],
        "entries": {},
    }  # fmt: skip
    reg_path = tmp_path / refl._REGISTRY
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    # schema falso
    reg_path.write_text(json.dumps({**base, "schema_version": 1}))
    assert any("schema_version" in p for p in refl.problems())
    # clave extra
    reg_path.write_text(json.dumps({**base, "backdoor": 1}))
    assert refl.problems() != []
    # JSON duplicado
    reg_path.write_text('{"schema_version": 2, "schema_version": 3}')
    assert any("duplicad" in p for p in refl.problems())
    # entrada obsoleta (ID que no existe en el codigo)
    reg_path.write_text(
        json.dumps(
            {
                **base,
                "entries": {
                    "deadbeef" * 8: {
                        "file": "m.py",
                        "qualname": "<module>",
                        "op": "getattr",
                        "statement_ast_sha256": "0" * 64,
                        "occurrence_index": 0,
                        "justification": "j",
                        "review_by": "2027-07-31",
                    }
                },
            }
        )
    )
    assert any("OBSOLETA" in p for p in refl.problems())


def test_b260_review_by_expiry_fails(tmp_path, monkeypatch):
    src = "def g(o, n):\n    return getattr(o, n)\n"
    _mount(tmp_path, monkeypatch, {"m.py": src})
    entries0, _ = refl.scan_reflection(["m.py"])
    eid, occ = next(iter(entries0.items()))
    entry = {k: occ[k] for k in ("file", "qualname", "op", "statement_ast_sha256", "occurrence_index")}
    reg = {
        "schema_version": refl._SCHEMA_VERSION, "scanner_version": refl._SCANNER_VERSION, "note": "x",
        "operations_controlled": list(refl.OPERATIONS_CONTROLLED), "authorized_campaign_bundle_importers": [],
        "entries": {eid: {**entry, "justification": "j", "review_by": "2000-01-01"}},
    }  # fmt: skip
    (tmp_path / refl._REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / refl._REGISTRY).write_text(json.dumps(reg))
    assert any("EXPIRADO" in p for p in refl.problems()), "review_by pasado debe fallar (B260)"


def test_b260_two_identical_occurrences_are_two_entries(tmp_path, monkeypatch):
    # dos referencias IDENTICAS en una funcion → dos entradas (occurrence_index 0 y 1), no un count agregado ambiguo.
    _mount(tmp_path, monkeypatch, {"m.py": "def g(o, n):\n    getattr(o, n)\n    getattr(o, n)\n"})
    entries, _ = refl.scan_reflection(["m.py"])
    idxs = sorted(e["occurrence_index"] for e in entries.values())
    assert idxs == [0, 1], f"dos ocurrencias identicas deben ser dos entradas indexadas (B260): {idxs}"


def test_b265_module_escapes_become_occurrences(tmp_path, monkeypatch):
    # B265: un módulo canónico que se fuga por contenedor/argumento/retorno/subscript-dinámico se vuelve una ocurrencia
    # (reflection-module-escape / builtins.dynamic-lookup), aunque no se conozca la operación final.
    cases = {
        "list_alias": "import builtins\nb = [builtins][0]\nb.getattr(o, n)\n",
        "tuple_unpack": "import builtins\nb, = (builtins,)\nb.setattr(o, n, v)\n",
        "nested_unpack": "import builtins\n(a, (b,)) = (1, (builtins,))\nb.getattr(o, n)\n",
        "dict_alias": "import builtins\nb = {'k': builtins}['k']\nb.getattr(o, n)\n",
        "dynamic_builtins": "__builtins__[dyn](o, n)\n",
        "attr_store": "import builtins\nclass C: pass\nc = C()\nc.m = builtins\n",
        "passed_to_fn": "import builtins\ndef take(m):\n    return m\ntake(builtins)\n",
        "returned": "import builtins\ndef give():\n    return builtins\ngive()\n",
        "identity_call": "import builtins\ndef ident(x):\n    return x\nb = ident(builtins)\n",
        "sys_container": "import sys\nxs = [sys]\n",
        "importlib_container": "import importlib as il\nxs = (il,)\n",
        "escape_in_lambda": "import builtins\nf = lambda: [builtins]\n",
        "escape_in_comprehension": "import builtins\nxs = [builtins for _ in range(1)]\n",
    }
    for label, src in cases.items():
        _mount(tmp_path, monkeypatch, {"m.py": src})
        ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
        # B316/B317: `__builtins__[dyn]` es ahora un lookup dinámico sobre builtins → PROHIBIDO (aún más fuerte que una
        # ocurrencia registrable); el resto siguen siendo escapes. En todos, el módulo canónico NO queda invisible.
        caught = {refl._REFLECTION_MODULE_ESCAPE, refl._BUILTINS_DYNAMIC_LOOKUP, refl._DYNAMIC_IMPORT_FACTORY_VALUE}
        assert ops & caught, f"{label} debe ser un escape/prohibido (B265/B317): {ops}"


def test_b265_no_false_positive_on_legit_module_use(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"m.py": "import sys\nimport functools\nsys.exit(0)\nfunctools.reduce(lambda a, b: a, [])\nprint(sys.version)\n"})  # fmt: skip
    ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert refl._REFLECTION_MODULE_ESCAPE not in ops, f"uso legítimo de módulo no debe escapar (B265): {ops}"


def test_b265_escape_in_authority_module_is_prohibited(tmp_path, monkeypatch):
    # un escape en un módulo de AUTORIDAD está prohibido (no registrable), incluso con un registro que lo listara.
    _mount(tmp_path, monkeypatch, {"tools/campaign_bundle.py": "import builtins\nxs = [builtins]\n"})
    reg = {
        "schema_version": refl._SCHEMA_VERSION, "scanner_version": refl._SCANNER_VERSION, "note": "x",
        "operations_controlled": list(refl.OPERATIONS_CONTROLLED), "authorized_campaign_bundle_importers": [],
        "entries": {},
    }  # fmt: skip
    (tmp_path / refl._REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / refl._REGISTRY).write_text(json.dumps(reg))
    assert any("PROHIBIDO" in p for p in refl.problems()), "escape en módulo de autoridad debe fallar (B265)"


def test_b265_unregistered_escape_fails_the_gate(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"m.py": "import builtins\nxs = [builtins]\n"})
    reg = {
        "schema_version": refl._SCHEMA_VERSION, "scanner_version": refl._SCANNER_VERSION, "note": "x",
        "operations_controlled": list(refl.OPERATIONS_CONTROLLED), "authorized_campaign_bundle_importers": [],
        "entries": {},
    }  # fmt: skip
    (tmp_path / refl._REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / refl._REGISTRY).write_text(json.dumps(reg))
    assert any("NO REGISTRADA" in p for p in refl.problems()), "un escape no registrado (fuera de autoridad) debe fallar (B265)"  # fmt: skip


def test_b270_rooted_chain_reproducing_module_is_escape(tmp_path, monkeypatch):
    # B270: una cadena con raíz en un módulo canónico que accede a MAQUINARIA de módulo (dunder/loader) y cuyo resultado
    # escapa → reflection-module-escape, aunque `alias.attr` parezca "comprendido".
    cases = {
        "loader_assign": "import builtins\nb = builtins.__spec__.loader.load_module('builtins')\n",
        "loader_return": "import builtins\ndef f():\n    return builtins.__spec__.loader.load_module('x')\n",
        "loader_arg": "import builtins\ntake(builtins.__spec__.loader.load_module('x'))\n",
        "find_spec_chain": "import importlib\nm = importlib.util.find_spec('x').loader.load_module('x')\n",
        "dunder_globals": "import sys\ng = sys.modules['__main__'].__dict__\n",  # sys.modules modeled → op detectado
    }
    for label, src in cases.items():
        _mount(tmp_path, monkeypatch, {"m.py": src})
        ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
        assert ops & {refl._REFLECTION_MODULE_ESCAPE, refl._BUILTINS_DYNAMIC_LOOKUP, "sys.modules", "__dict__"}, f"{label} debe ser una ocurrencia (B270): {ops}"  # fmt: skip


def test_b270_data_attribute_chains_are_not_escapes(tmp_path, monkeypatch):
    # control anti-falso-positivo: cadenas de DATOS (version/argv/path/stderr/exit) NO se marcan como escape.
    src = (
        "import sys\n"
        "v = sys.version.split()[0]\n"
        "a = sys.argv\n"
        "sys.exit(1)\n"
        "sys.stderr.write('x')\n"
        "sys.path.insert(0, 'x')\n"
        "print(sys.version, sys.platform, sys.executable)\n"
    )
    _mount(tmp_path, monkeypatch, {"m.py": src})
    ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert refl._REFLECTION_MODULE_ESCAPE not in ops, f"cadenas de datos sys.* no deben escapar (B270): {ops}"


def test_b270_chain_escape_in_authority_is_prohibited(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"tools/campaign_bundle.py": "import builtins\nb = builtins.__spec__.loader.load_module('x')\n"})  # fmt: skip
    reg = {
        "schema_version": refl._SCHEMA_VERSION, "scanner_version": refl._SCANNER_VERSION, "note": "x",
        "operations_controlled": list(refl.OPERATIONS_CONTROLLED), "authorized_campaign_bundle_importers": [],
        "entries": {},
    }  # fmt: skip
    (tmp_path / refl._REGISTRY).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / refl._REGISTRY).write_text(json.dumps(reg))
    assert any("PROHIBIDO" in p for p in refl.problems()), "cadena reflexiva en módulo de autoridad debe fallar (B270)"


def test_reflection_gate_catches_the_seven_evasions(tmp_path, monkeypatch):
    evasions = {
        "list_index_getattr": "g = [getattr][0]\ng(cb, name)\n",
        "partial_alias": "import functools\np = functools.partial\np(getattr, cb)(name)\n",
        "import_module_dyn": "import importlib\nimportlib.import_module(modname)\n",
        "sys_modules_dyn": "import sys\nsys.modules[modname]\n",
        "dunder_import_dyn": "__import__(modname)\n",
        "own_wrapper": "def reflect(obj, name):\n    return getattr(obj, name)\nreflect(cb, name)\n",
        "builtins_dict": "import builtins\nbuiltins.__dict__[name]\n",
    }
    for label, body in evasions.items():
        _mount(
            tmp_path,
            monkeypatch,
            {
                f"{label}.py": body,
                refl._REGISTRY: json.dumps(
                    {
                        "schema_version": refl._SCHEMA_VERSION,
                        "scanner_version": refl._SCANNER_VERSION,
                        "note": "x",
                        "operations_controlled": list(refl.OPERATIONS_CONTROLLED),
                        "authorized_campaign_bundle_importers": [],
                        "entries": {},
                    }
                ),
            },
        )
        monkeypatch.setattr(refl, "_production_files", lambda lbl=label: [f"{lbl}.py"])
        # cada evasión falla el gate: NO REGISTRADA (registrable) o PROHIBIDA (fábrica dinámica como valor, B310)
        assert any("NO REGISTRADA" in p or "PROHIBID" in p for p in refl.problems()), f"evasion {label} debe fallar (B255)"  # fmt: skip


# ---------------------------------------------------------------------------
# B276 — una CADENA reflexiva descartada como `Expr` puede tener EFECTO (cargar/importar/mutar) y no basta con marcar
# sólo cuando el valor escapa. RED_BASE_SHA = 036c8f9: el retorno temprano exime todo `ast.Expr`, así que un
# `builtins.__spec__.loader.load_module('builtins')` como statement se aceptaba (0 ocurrencias). Estas pruebas usan
# scan_reflection (API estable en ambos SHAs): en 036c8f9 el caso descartado NO se marca (RED), aquí sí.
# ---------------------------------------------------------------------------
def test_b276_discarded_reflective_call_is_escape(tmp_path, monkeypatch):
    cases = {
        "load_module_expr": "import builtins\nbuiltins.__spec__.loader.load_module('builtins')\n",
        "exec_module_expr": "import importlib\nimportlib.util.find_spec('x').loader.exec_module(m)\n",
        "loader_reload_expr": "import importlib\nimportlib.__loader__.load_module('x')\n",
        "chain_then_setattr": "import builtins\nbuiltins.__spec__.loader.load_module('os').setattr(o, n, v)\n",
    }
    for label, src in cases.items():
        _mount(tmp_path, monkeypatch, {"m.py": src})
        ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
        assert refl._REFLECTION_MODULE_ESCAPE in ops, f"{label} (efecto descartado) debe marcarse (B276): {ops}"


def test_b276_benign_discarded_dunder_call_not_escape(tmp_path, monkeypatch):
    # traversa un dunder pero la llamada final NO es maquinaria/terminal y el resultado se descarta → NO escape (anti-FP).
    for src in ("import builtins\nbuiltins.__doc__.strip()\n", "import builtins\nbuiltins.__name__.upper()\n"):
        _mount(tmp_path, monkeypatch, {"m.py": src})
        ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
        assert refl._REFLECTION_MODULE_ESCAPE not in ops, f"llamada benigna descartada NO debe escapar (B276): {ops}"


def test_b276_preserves_b270_escape_when_result_used(tmp_path, monkeypatch):
    # regresión: el escape por valor (asignado/retornado/pasado) sigue marcándose (no romper B270).
    for src in (
        "import builtins\nx = builtins.__spec__.loader.load_module('builtins')\n",
        "import builtins\ndef f():\n    return builtins.__spec__.loader.load_module('x')\n",
    ):
        _mount(tmp_path, monkeypatch, {"m.py": src})
        ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
        assert refl._REFLECTION_MODULE_ESCAPE in ops, f"escape por valor debe seguir marcándose (B276/B270): {ops}"


def test_b276_data_sys_calls_still_not_flagged(tmp_path, monkeypatch):
    # control: llamadas de DATOS sys.* descartadas siguen sin marcarse (no nueva regresión de falsos positivos).
    src = "import sys\nsys.exit(1)\nsys.stderr.write('x')\nsys.version.split()[0]\n"
    _mount(tmp_path, monkeypatch, {"m.py": src})
    ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert refl._REFLECTION_MODULE_ESCAPE not in ops, f"llamadas de datos sys.* no deben escapar (B276): {ops}"


# ---------------------------------------------------------------------------
# B285 — política POSITIVA: TODA llamada rooteada en un módulo canónico produce una ocurrencia registrable. RED_BASE_SHA
# = b781d68: el scanner seguía LISTAS de terminales (_CHAIN_DANGER_NAMES/_CHAIN_CALL_DANGER), así que un terminal no
# enumerado (SourceFileLoader.set_data, sys.meta_path.insert, sys.path_hooks.append, importlib.invalidate_caches) era
# INVISIBLE. Las pruebas usan scan_reflection (API estable): en b781d68 esos casos dan {} (RED); aquí producen ocurrencia.
# ---------------------------------------------------------------------------
def test_b285_unenumerated_terminals_now_flagged(tmp_path, monkeypatch):
    cases = {
        "set_data": "import importlib\nimportlib.machinery.SourceFileLoader('x', 'x').set_data('/tmp/pwn', b'x')\n",
        "meta_path_insert": "import sys\nsys.meta_path.insert(0, hook)\n",
        "path_hooks_append": "import sys\nsys.path_hooks.append(hook)\n",
        "invalidate_caches": "import importlib\nimportlib.invalidate_caches()\n",
    }
    for label, src in cases.items():
        _mount(tmp_path, monkeypatch, {"m.py": src})
        occ = refl.scan_reflection(["m.py"])[0]
        assert occ, f"{label} debe producir una ocurrencia (B285); vacío en b781d68"


def test_b285_op_is_canonical_rooted_call(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"m.py": "import sys\nsys.meta_path.insert(0, x)\n"})
    ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert refl._CANONICAL_ROOTED_CALL in ops, f"debe marcarse canonical-rooted-call (B285): {ops}"


def test_b285_data_and_modeled_ops_still_specific(tmp_path, monkeypatch):
    # una op MÁS ESPECÍFICA gana: getattr sigue siendo getattr (no canonical-rooted-call).
    _mount(tmp_path, monkeypatch, {"m.py": "import builtins\nbuiltins.getattr(o, n)\n"})
    ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert "getattr" in ops, f"getattr debe mantener su op específica (B285): {ops}"


def test_b285_non_canonical_call_not_flagged(tmp_path, monkeypatch):
    # una llamada rooteada en un módulo NO canónico (os) NO produce ocurrencia.
    _mount(tmp_path, monkeypatch, {"m.py": "import os\nos.path.join('a', 'b')\n"})
    assert refl.scan_reflection(["m.py"])[0] == {}, "os.path.join no debe marcarse (B285)"


# ---------------------------------------------------------------------------
# B289 — el scanner ve submódulos/clases/fábricas importados DESDE importlib (no sólo `import importlib`). RED_BASE_SHA =
# b781d68: `from importlib import machinery`, `import importlib.machinery as m`, `from importlib.machinery import
# SourceFileLoader as L` daban entries=0, problems=[]. Las pruebas usan scan_reflection (API estable): en b781d68 dan {}
# (RED); aquí producen ocurrencia.
# ---------------------------------------------------------------------------
def test_b289_importlib_submodule_and_from_imports_flagged(tmp_path, monkeypatch):
    cases = {
        "from_importlib_machinery": "from importlib import machinery\nmachinery.SourceFileLoader('x', 'x').set_data('/tmp/pwn', b'x')\n",  # fmt: skip
        "import_submodule_as": "import importlib.machinery as machinery\nmachinery.SourceFileLoader('x', 'x').set_data('/tmp/pwn', b'x')\n",  # fmt: skip
        "from_machinery_class_alias": "from importlib.machinery import SourceFileLoader as Loader\nLoader('x', 'x').set_data('/tmp/pwn', b'x')\n",  # fmt: skip
        "factory_result_binding": "import importlib\nm = importlib.import_module('os')\nm.system('id')\n",
        "from_metadata": "from importlib.metadata import version\nversion('pkg')\n",
    }
    for label, src in cases.items():
        _mount(tmp_path, monkeypatch, {"m.py": src})
        occ = refl.scan_reflection(["m.py"])[0]
        assert occ, f"{label} debe producir una ocurrencia (B289); vacío en b781d68"


def test_b289_op_is_canonical_rooted_call(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch, {"m.py": "from importlib.machinery import SourceFileLoader as L\nL('x', 'x').set_data('/tmp/p', b'x')\n"})  # fmt: skip
    ops = {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()}
    assert refl._CANONICAL_ROOTED_CALL in ops, f"debe ser canonical-rooted-call (B289): {ops}"


def test_b289_prim_from_import_keeps_specific_op(tmp_path, monkeypatch):
    # un `from builtins import getattr as g` sigue siendo la op ESPECÍFICA getattr, NO canonical-rooted-call (anti-doble).
    _mount(tmp_path, monkeypatch, {"m.py": "from builtins import getattr as g\ng(o, n)\n"})
    assert {e["op"] for e in refl.scan_reflection(["m.py"])[0].values()} == {"getattr"}


def test_b289_non_canonical_from_import_not_flagged(tmp_path, monkeypatch):
    # un `from os.path import join` (no canónico) NO se marca.
    _mount(tmp_path, monkeypatch, {"m.py": "from os.path import join\njoin('a', 'b')\n"})
    assert refl.scan_reflection(["m.py"])[0] == {}, "un from-import no canónico no debe marcarse (B289)"


# ---------------------------------------------------------------------------
# B294 — el modelo de raíces canónicas pasa de una lista de aliases a un LATTICE de PROCEDENCIA por fixpoint que sigue:
# RESULTADOS de fábrica (`__import__`/`import_module` bare, aliased por from-import, o attr-alias `f =
# importlib.import_module`, incluso a varios saltos), `from <canónico> import *` (rechazado), alias de `sys.modules` y
# `<canónico>.__dict__` importados y SUBSCRIPTADOS, y la CAPTURA NO-CALL de un miembro `from <canónico> import <m>`.
# RED_BASE_SHA = 731b6d2: cada terminal quedaba INVISIBLE (el binding del resultado de fábrica no se rooteaba, el import*
# no se rechazaba, los alias subscriptados y el escape de miembro daban 0 ocurrencias). Verificado RED-conductual en el
# worktree 731b6d2: la LÍNEA del terminal da {} allí y el op específico aquí. Las pruebas usan scan_reflection (API
# estable en ambos SHAs) y afirman el OP ESPECÍFICO en la LÍNEA del terminal — no `assert entries` genérico.
# ---------------------------------------------------------------------------
def _line_ops(tmp_path, monkeypatch, src: str, line: int) -> set[str]:
    _mount(tmp_path, monkeypatch, {"m.py": src})
    occ = refl.scan_reflection(["m.py"])[0]
    return {o["op"] for o in occ.values() if o["lineno"] == line}


def test_b294_factory_result_binding_is_prohibited(tmp_path, monkeypatch):
    # B308: un resultado de fábrica DINÁMICA ligado a un binding (bare __import__, from-import alias, attr-alias, a 3
    # saltos) es `dynamic-module-result-escape` PROHIBIDO (deny-by-default), no se rastrea como rooted-call.
    cases = {
        "bare_import": "m = __import__('os')\nr = m.system('x')\n",
        "from_import": "from importlib import import_module as im\nm = im('os')\ny = m.getcwd()\n",
        "attr_alias": "import importlib\nf = importlib.import_module\nm = f('os')\ny = m.getcwd()\n",
        "alias_3_hop": "from importlib import import_module as im\na = im\nb = a\nm = b('os')\ny = m.getcwd()\n",
    }
    for label, src in cases.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops, f"{label}: el binding de fábrica debe estar prohibido (B308): {ops}"  # fmt: skip


def test_b294_import_star_of_canonical_is_rejected(tmp_path, monkeypatch):
    # `from importlib import *` ROMPE la procedencia (no se puede seguir qué queda rooteado) → problema fail-closed.
    _mount(tmp_path, monkeypatch, {"m.py": "from importlib import *\n"})
    probs = refl.scan_reflection(["m.py"])[1]
    assert any("import *" in p and "B294" in p for p in probs), (
        f"from importlib import * debe rechazarse (B294): {probs}"
    )


def test_b294_canonical_alias_subscripts_flagged(tmp_path, monkeypatch):
    # `from sys import modules as mods; mods[k]` da su op modelada `sys.modules` (registrable) — invisible en 731b6d2.
    assert "sys.modules" in _line_ops(tmp_path, monkeypatch, "from sys import modules as mods\nx = mods['os']\n", 2)
    # B316/B317: `from builtins import __dict__ as ns; ns[k]` es un lookup dinámico sobre el namespace de builtins →
    # PROHIBIDO (globalmente no registrable), no un `__dict__` registrable: `ns[k]` puede resolver a `__import__`.
    assert refl._DYNAMIC_IMPORT_FACTORY_VALUE in _line_ops(tmp_path, monkeypatch, "from builtins import __dict__ as ns\nx = ns['open']\n", 2)  # fmt: skip


def test_b294_member_noncall_escape(tmp_path, monkeypatch):
    # un miembro `from <canónico> import <m>` CAPTURADO como valor (bare/contenedor/default/retorno/closure) — no
    # llamado, ni `.attr`, ni subscriptado — escapa. En 731b6d2 daban 0 ocurrencias (RED).
    cases = {
        "bare": ("from importlib import machinery\nx = machinery\n", 2),
        "container": ("from importlib import machinery\nxs = [machinery]\n", 2),
        "default_arg": ("from importlib import machinery\ndef f(x=machinery):\n    return x\n", 2),
        "returned": ("from importlib import machinery\ndef f():\n    return machinery\n", 3),
        "closure": (
            "from importlib import machinery\ndef outer():\n    def inner():\n        return machinery\n    return inner\n",
            4,
        ),  # fmt: skip
    }
    for label, (src, line) in cases.items():
        assert refl._REFLECTION_MODULE_ESCAPE in _line_ops(tmp_path, monkeypatch, src, line), f"{label}: captura no-call de miembro debe escapar (B294)"  # fmt: skip


def test_b294_benign_member_calls_not_over_flagged(tmp_path, monkeypatch):
    # REGRESIÓN de la sobre-detección que introdujo el lattice: un miembro canónico LLAMADO (decorador `@lru_cache(...)`,
    # `version(...)`) es canonical-rooted-call, NUNCA reflection-module-escape — el CALLEE de un Call no es un escape,
    # sólo un miembro pasado como ARGUMENTO o capturado como valor lo es.
    for label, src, line in [
        ("lru_decorator", "from functools import lru_cache\n@lru_cache(maxsize=8)\ndef f():\n    return 1\n", 2),
        ("version_call", "from importlib.metadata import version\nv = version('pkg')\n", 2),
    ]:
        ops = _line_ops(tmp_path, monkeypatch, src, line)
        assert refl._CANONICAL_ROOTED_CALL in ops, f"{label}: miembro llamado debe ser rooted-call (B294): {ops}"
        assert refl._REFLECTION_MODULE_ESCAPE not in ops, f"{label}: miembro llamado NO debe escapar (B294): {ops}"


def test_b294_specific_and_noncanonical_ops_preserved(tmp_path, monkeypatch):
    # `partial` mantiene su op ESPECÍFICA (no rooted-call ni escape); un from-import NO canónico capturado como valor no
    # se marca (anti-falso-positivo del lattice).
    assert _line_ops(tmp_path, monkeypatch, "from functools import partial as p\np(f, 1)\n", 2) == {"partial"}
    _mount(tmp_path, monkeypatch, {"m.py": "from os.path import join\nx = join\n"})
    assert refl.scan_reflection(["m.py"])[0] == {}, "un from-import no canónico capturado no debe marcarse (B294)"


# ---------------------------------------------------------------------------
# B297 — la procedencia de fábrica sólo se propagaba por bindings `Name = Name|Call` simples: el RESULTADO de una fábrica
# usado en una EXPRESIÓN COMPUESTA (cadena directa, alias de fábrica, asignación múltiple, contenedor+subscript,
# condicional, walrus) era invisible. En 03f8e3b la llamada terminal `.system(...)` sobre el módulo importado NO aparecía
# como canonical-rooted-call (RED). El dominio abstracto de expresión (`_expr_provenance`) la sigue ahora.
# ---------------------------------------------------------------------------
def test_b297_factory_result_through_composite_expressions(tmp_path, monkeypatch):
    # B308: en TODAS estas formas compuestas la fábrica dinámica es `dynamic-module-result-escape` PROHIBIDO — no se
    # rastrea el resultado (deny-by-default; el único descarte seguro es `import_module(...)` como statement).
    cases = {
        "direct_chain": "__import__('os').system('x')\n",
        "aliased_factory": "f = __import__\nf('os').system('x')\n",
        "multi_assign": "a = b = __import__('os')\na.system('x')\n",
        "tuple_subscript": "m = (__import__('os'),)[0]\nm.system('x')\n",
        "dict_subscript": "m = {'x': __import__('os')}['x']\nm.system('x')\n",
        "conditional": "m = __import__('os') if flag else None\nm.system('x')\n",
        "import_module_chain": "from importlib import import_module\nimport_module('os').system('x')\n",
        "alias_3_hop": "f = __import__\ng = f\nh = g\nh('os').system('x')\n",
        "destructuring": "(m, n) = (__import__('os'), 1)\nm.system('x')\n",
        "walrus_inline": "(m := __import__('os')).system('x')\n",
        "attr_of_factory_result": "m = __import__('os')\np = m.path\np.join('a', 'b')\n",
    }
    for label, src in cases.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops, f"{label}: la fábrica compuesta debe estar prohibida (B308): {ops}"  # fmt: skip


def test_b297_benign_and_specific_ops_preserved(tmp_path, monkeypatch):
    # controles benignos del dominio de expresión: un módulo NO canónico y un escalar no se marcan; una op ESPECÍFICA
    # (getattr) no se degrada a rooted-call; `version()`/`lru_cache()` siguen sin escape duplicado.
    assert _line_ops(tmp_path, monkeypatch, "import os\nx = os.getcwd()\n", 2) == set()
    assert _line_ops(tmp_path, monkeypatch, "m = 5\nm.bit_length()\n", 2) == set()
    assert _line_ops(tmp_path, monkeypatch, "from builtins import getattr as g\ng(o, n)\n", 2) == {"getattr"}
    ops = _line_ops(tmp_path, monkeypatch, "from importlib.metadata import version\nv = version('p')\n", 2)
    assert ops == {refl._CANONICAL_ROOTED_CALL}, f"version() sin escape duplicado (B297): {ops}"


# ---------------------------------------------------------------------------
# B300 — el dominio sólo tenía value/factory/none: un resultado de fábrica que atravesaba una transformación DESCONOCIDA
# (`ident(...)`, `next(iter([...]))`) o superaba el tope de profundidad se degradaba a `none` y la terminal quedaba
# INVISIBLE SIN MARCA (silencioso). Ahora la pérdida de precisión produce `reflection-module-escape` en el punto exacto,
# o un PROBLEMA fail-closed en el tope; un nesting realista (61/65/100) se analiza por completo.
# ---------------------------------------------------------------------------
def _ops_and_problems(tmp_path, monkeypatch, src):
    _mount(tmp_path, monkeypatch, {"m.py": src})
    occ, probs = refl.scan_reflection(["m.py"])
    return {o["op"] for o in occ.values()}, probs


def test_b300_unknown_transformation_of_factory_result_escapes(tmp_path, monkeypatch):
    # B308: un resultado de fábrica consumido por una transformación desconocida → `dynamic-module-result-escape`.
    cases = {
        "unknown_call_arg": "def ident(x):\n    return x\nm = ident(__import__('os'))\nm.system('x')\n",
        "next_iter_list": "m = next(iter([__import__('os')]))\nm.system('x')\n",
        "list_escapes_to_call": "foo([__import__('os')])\n",
        "tuple_escapes_to_call": "consume((__import__('os'),))\n",
        "dict_value_escape": "reg = {'m': __import__('os')}\n",
    }
    for label, src in cases.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops, f"{label}: la pérdida debe estar prohibida (B308): {ops}"


def test_b300_deep_nesting_factory_still_prohibited(tmp_path, monkeypatch):
    # B308: la profundidad ya NO es un problema — el clasificador mira el PADRE INMEDIATO de la llamada de fábrica (sin
    # recursión), así que una fábrica dinámica anidada 65/100/300 niveles sigue siendo `dynamic-module-result-escape`.
    for depth in (65, 100, 300):
        src = "m = obj" + "".join(f"[{i}]" for i in range(depth)) + "\nx = __import__('os')\n"
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops, f"nesting {depth}: fábrica sigue prohibida (B308): {ops}"


def test_b300_over_cap_is_fail_closed_problem(tmp_path, monkeypatch):
    # superar la cota de análisis NO se degrada a `none`: es un PROBLEMA fail-closed. Cadena de atributos de 300 niveles
    # (sin paréntesis → sin el límite del parser) que hace recursar `_expr_provenance` más allá de la cota.
    src = "m = obj" + ".a" * 300 + "\n"
    _, probs = _ops_and_problems(tmp_path, monkeypatch, src)
    assert any("B300" in p for p in probs), f"el tope de análisis debe ser un problema fail-closed (B300): {probs}"


def test_b300_no_over_fire_on_data_attributes_and_non_factory(tmp_path, monkeypatch):
    # controles: un ATRIBUTO DE DATOS (`sys.version`) o un constructor NO-fábrica pasados a una llamada, y contenedores
    # de escalares, NO se marcan como escape (la regla es NARROW a resultados de fábrica).
    for src in (
        "import sys\nprint(sys.version)\n",
        "import sys\nsys.exit(0)\n",
        "from importlib import machinery\nfoo(machinery.SourceFileLoader('a', 'b'))\n",
        "xs = [1, 2, 3]\nfoo(xs)\n",
        "foo(bar, baz)\n",
    ):
        ops, probs = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._REFLECTION_MODULE_ESCAPE not in ops and not probs, (
            f"no debe sobre-disparar (B300): {src!r} → {ops}"
        )


# ---------------------------------------------------------------------------
# B305 — un resultado de fábrica que sale por `return`/`yield`/`yield from`, store a ATRIBUTO o a SUBSCRIPT, o
# `AnnAssign`/`AugAssign` a esos destinos sólo registraba la fábrica; el punto donde el valor ABANDONA el dominio
# modelado quedaba sin `reflection-module-escape`. Ahora cada sink emite el escape en su frontera de pérdida.
# ---------------------------------------------------------------------------
def test_b305_canonical_escape_recorded_at_every_sink(tmp_path, monkeypatch):
    cases = {
        "return_direct": "def get():\n    return __import__('os')\n",
        "yield_direct": "def gen():\n    yield __import__('os')\n",
        "yield_from": "def gen():\n    yield from [__import__('os')]\n",
        "attr_store": "holder.module = __import__('os')\n",
        "subscript_store": "holder['module'] = __import__('os')\n",
        "annassign_attr": "holder.mod: object = __import__('os')\n",
        "augassign_attr": "holder.mod += __import__('os')\n",
        "return_bound_name": "def g():\n    m = __import__('os')\n    return m\n",
        "import_module_attr_store": "from importlib import import_module\nh.m = import_module('os')\n",
    }
    for label, src in cases.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops, f"{label}: el sink debe estar prohibido (B308): {ops}"


def test_b305_comprehension_and_generator_sinks(tmp_path, monkeypatch):
    # B308: un resultado de fábrica dentro de una comprensión/generador cae por default-deny → prohibido.
    cases = {
        "listcomp": "xs = [__import__('os') for _ in range(1)]\n",
        "setcomp": "s = {__import__('os') for _ in range(1)}\n",
        "genexp": "g = (__import__('os') for _ in range(1))\n",
        "dictcomp_value": "d = {k: __import__('os') for k in range(1)}\n",
        "comp_iter": "[x for x in __import__('os')]\n",
    }
    for label, src in cases.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops, f"{label}: la comprensión/generador debe estar prohibida (B308): {ops}"  # fmt: skip
    # control: comprensión de escalares no dispara
    ops, probs = _ops_and_problems(tmp_path, monkeypatch, "xs = [i for i in range(3)]\n")
    assert refl._DYNAMIC_MODULE_RESULT_ESCAPE not in ops and not probs


def test_b308_deny_by_default_covers_unenumerated_contexts(tmp_path, monkeypatch):
    # B308: contextos que NINGUNA lista de sinks enumeraba caen automáticamente por default-deny → prohibido.
    for label, src in {
        "await": "async def f():\n    await __import__('os')\n",
        "with": "with __import__('os'):\n    pass\n",
        "raise": "raise __import__('os')\n",
        "lambda_body": "f = lambda: __import__('os')\n",
        "default_arg": "def f(x=__import__('os')):\n    return x\n",
        "assert_test": "assert __import__('os')\n",
        "fstring": "s = f'{__import__(\"os\")}'\n",
        "starred": "foo(*[__import__('os')])\n",
    }.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops, f"{label}: default-deny debe prohibir (B308): {ops}"


def test_b308_b316_no_safe_pattern_exists(tmp_path, monkeypatch):
    # B316/B317: YA NO hay patrón autorizado — `deep_smoke` importa su stack de forma estática, así que producción no
    # requiere NINGUNA fábrica de import dinámico. Incluso `importlib.import_module(...)` DESCARTADO es prohibido ahora.
    ops, probs = _ops_and_problems(tmp_path, monkeypatch, "import importlib\nimportlib.import_module(name)\n")
    assert refl._DYNAMIC_IMPORT_FACTORY_VALUE in ops and "import_module" not in ops, ops
    # `__import__` tampoco tiene patrón autorizado: incluso descartado, es prohibido.
    ops, _ = _ops_and_problems(tmp_path, monkeypatch, "__import__('os')\n")
    assert refl._DYNAMIC_MODULE_RESULT_ESCAPE in ops or refl._DYNAMIC_IMPORT_FACTORY_VALUE in ops, ops
    # gate: una fábrica prohibida NO se puede blanquear con una entrada de registro (globalmente prohibida).
    _mount(
        tmp_path,
        monkeypatch,
        {
            "m.py": "raise __import__('os')\n",
            refl._REGISTRY: json.dumps(
                {
                    "schema_version": refl._SCHEMA_VERSION,
                    "scanner_version": refl._SCANNER_VERSION,
                    "note": "x",
                    "operations_controlled": list(refl.OPERATIONS_CONTROLLED),
                    "authorized_campaign_bundle_importers": [],
                    "entries": {},
                }
            ),
        },
    )
    monkeypatch.setattr(refl, "_production_files", lambda: ["m.py"])
    assert any("B308" in p and "PROHIBID" in p for p in refl.problems())


# ---------------------------------------------------------------------------
# B310 — B308 sólo protegía la LLAMADA a la fábrica; una fábrica tratada como VALOR de primera clase (contenedor,
# condicional, lambda, partial, alias, from-import) evadía el gate y se llamaba después de forma no reconocible. Ahora un
# contrato SINTÁCTICO POSITIVO prohíbe la fábrica como valor: `dynamic-import-factory-value`.
# ---------------------------------------------------------------------------
def test_b310_factory_as_value_is_prohibited(tmp_path, monkeypatch):
    cases = {
        "container": "f = [__import__][0]\nm = f('os')\nm.system('x')\n",
        "conditional": "f = __import__ if flag else __import__\nm = f('os')\n",
        "lambda_returns": "f = (lambda: __import__)()\nm = f('os')\n",
        "next_iter": "m = next(iter([__import__]))\n",
        "tuple_capture": "t = (__import__, 1)\n",
        "dict_capture": "d = {'f': __import__}\n",
        "partial": "import functools\np = functools.partial(__import__)\n",
        "return_value": "def g():\n    return __import__\n",
        "default_value": "def g(f=__import__):\n    return f\n",
        "from_import_module": "from importlib import import_module\nx = import_module\n",
        "from_import_dunder": "from importlib import __import__ as f\n",
        "attr_capture": "import importlib\nf = importlib.import_module\n",
        "attr_capture_arg": "import importlib\nfoo(importlib.import_module)\n",
    }
    for label, src in cases.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_IMPORT_FACTORY_VALUE in ops, (
            f"{label}: la fábrica como valor debe prohibirse (B310): {ops}"
        )


def test_b316_no_safe_form_and_benign_controls(tmp_path, monkeypatch):
    # B316/B317: `importlib.import_module(...)` DESCARTADO ya NO es forma segura → prohibido.
    ops, _ = _ops_and_problems(tmp_path, monkeypatch, "import importlib\nimportlib.import_module(name)\n")
    assert refl._DYNAMIC_IMPORT_FACTORY_VALUE in ops, ops
    # controles benignos: no sobredisparan (incl. `importlib.metadata.version`, `getattr` sobre no-canónico).
    for src in (
        "import sys\nx = sys.version\n",
        "from importlib.metadata import version\nv = version('p')\n",
        "import importlib.metadata\nv = importlib.metadata.version('p')\n",
        "import functools\np = functools.partial(foo)\n",
        "import os\nx = os.getcwd()\n",
        "import os\nx = getattr(os, 'getcwd')\n",
    ):
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_IMPORT_FACTORY_VALUE not in ops, f"no debe sobredisparar (B316/B317): {src!r} → {ops}"


def test_b317_lookup_over_factory_module_prohibited(tmp_path, monkeypatch):
    # B316/B317: todo lookup dinámico sobre builtins/importlib está prohibido — nombre LITERAL o CALCULADO — mientras que
    # el mismo lookup sobre un objeto NO canónico sigue siendo un `getattr` registrable (contrato ligado al OBJETO).
    for label, src in {
        "rebind": "import importlib\nimportlib = None\nimportlib.import_module('x')\n",
        "getattr_literal": "import builtins\ng = getattr(builtins, '__import__')\n",
        "getattr_computed": "import builtins\ng = getattr(builtins, 'import' + '_module')\n",
        "vars_literal": "import builtins\nf = vars(builtins)['__import__']\n",
        "vars_computed": "import builtins\nname = 'x'\nf = vars(builtins)[name]\n",
        "dict_literal": "import builtins\nf = builtins.__dict__['__import__']\n",
        "il_getattr_computed": "import importlib\nname = 'import' + '_module'\nf = getattr(importlib, name)\n",
        "attrgetter_over_builtins": "import operator, builtins\nn = 'x'\nf = operator.attrgetter(n)(builtins)\n",
        "partial_getattr_builtins": "import functools, builtins\nn = 'x'\nf = functools.partial(getattr, builtins, n)\n",
    }.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_IMPORT_FACTORY_VALUE in ops, f"{label}: debe prohibirse (B316/B317): {ops}"
    for label, src in {
        "frozenset_literal": 'F = frozenset({"import_module", "__import__"})\n',
        "getattr_noncanonical": "g = getattr(m, 'import_module')\n",  # m no es builtins/importlib → registrable
        "normal_getattr": "g = getattr(obj, 'normal_attr')\n",
        "getattr_os": "import os\ng = getattr(os, 'getcwd')\n",  # os no expone fábricas de import dinámico
        "normal_dict": "d = {'__import__': 1}\nx = d['k']\n",
    }.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_IMPORT_FACTORY_VALUE not in ops, f"{label}: no debe sobredisparar (B316/B317): {ops}"


def test_b316_shadowing_no_longer_creates_a_safe_form(tmp_path, monkeypatch):
    # B316: en 552fe3c la forma segura confiaba en `clean_importlib` (una tabla de nombres SIN scope léxico), así que un
    # parámetro/default/local llamado `importlib` sombreaba al módulo real y `importlib.import_module(name)` se declaraba
    # SEGURO. Al eliminar la forma segura, `importlib.import_module` es prohibido SIEMPRE — el shadowing ya no ayuda ni
    # crea un falso-seguro.
    for label, src in {
        "param_shadow": "import importlib\ndef f(importlib):\n    importlib.import_module(name)\n",
        "default_shadow": "import importlib\ndef f(importlib=evil):\n    importlib.import_module(name)\n",
        "local_shadow": "import importlib\ndef f():\n    importlib = None\n    return importlib.import_module(name)\n",
        "alias_attr": "import importlib as il\nx = il.import_module\n",
        "metadata_plus_factory": "import importlib.metadata\nx = importlib.import_module\n",
    }.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_IMPORT_FACTORY_VALUE in ops, f"{label}: debe prohibirse (B316): {ops}"


def test_b317_import_transform_prohibited_at_the_source(tmp_path, monkeypatch):
    # B317: obtener la fábrica y luego TRANSFORMARLA (contenedor/identidad) evadía porque sólo se marcaba la llamada. Al
    # prohibir en el ORIGEN sintáctico (ImportFrom/Name), la transformación posterior es irrelevante: el programa ya es
    # inválido donde OBTIENE la fábrica, aunque nunca la llame.
    for label, src in {
        "from_builtins_unused": "from builtins import __import__ as f\n",
        "from_builtins_transformed": "from builtins import __import__ as f\ng = [f][0]\nm = g('os')\n",
        "from_importlib_unused": "from importlib import import_module as im\n",
        "dunder_builtins_name": "x = __builtins__\n",
        "dunder_builtins_subscript": "f = __builtins__['__import__']\n",
    }.items():
        ops, _ = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._DYNAMIC_IMPORT_FACTORY_VALUE in ops, f"{label}: debe prohibirse en el origen (B317): {ops}"


def test_b316_deep_smoke_imports_its_stack_statically():
    # B316: `tools/deep_smoke.py` YA NO usa `importlib.import_module`; los imports del stack son ESTÁTICOS y el conjunto de
    # nombres importados dentro de `run()` es EXACTAMENTE `set(STACK)` (sin duplicar la lista de ocho en dos lugares).
    import ast
    import pathlib

    import tools.deep_smoke as ds

    src = pathlib.Path(ds.__file__).read_text()
    tree = ast.parse(src)
    # AST, no substring: la docstring MENCIONA `importlib.import_module` legítimamente; lo que se prohíbe es el ACCESO.
    dyn = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("import_module", "__import__")]
    assert not dyn, "deep_smoke no debe ACCEDER .import_module/.__import__ (B316)"
    run_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run")
    local_imports = {a.name for n in ast.walk(run_fn) if isinstance(n, ast.Import) for a in n.names}
    assert local_imports == set(ds.STACK), f"imports locales de run() {local_imports} != set(STACK) {set(ds.STACK)}"


def test_b310_prohibited_value_fails_the_gate(tmp_path, monkeypatch):
    _mount(
        tmp_path,
        monkeypatch,
        {
            "m.py": "f = [__import__][0]\n",
            refl._REGISTRY: json.dumps(
                {
                    "schema_version": refl._SCHEMA_VERSION,
                    "scanner_version": refl._SCANNER_VERSION,
                    "note": "x",
                    "operations_controlled": list(refl.OPERATIONS_CONTROLLED),
                    "authorized_campaign_bundle_importers": [],
                    "entries": {},
                }
            ),
        },
    )
    monkeypatch.setattr(refl, "_production_files", lambda: ["m.py"])
    assert any("B310" in p and "PROHIBID" in p for p in refl.problems())


def test_b305_benign_sinks_not_over_flagged(tmp_path, monkeypatch):
    # controles: escalares o atributos de datos por los mismos sinks NO se marcan; un `m = factory()` a Name simple se
    # RASTREA (no escapa aquí); un store a Name simple de escalar es benigno.
    for src in (
        "def g():\n    return 5\n",
        "import sys\ndef g():\n    return sys.version\n",
        "m = __import__('os')\n",  # target Name → rooteado y rastreado, sin escape en el binding
        "holder.x = 5\n",
        "d['k'] = 42\n",
    ):
        ops, probs = _ops_and_problems(tmp_path, monkeypatch, src)
        assert refl._REFLECTION_MODULE_ESCAPE not in ops and not probs, (
            f"no debe sobre-disparar (B305): {src!r} → {ops}"
        )
