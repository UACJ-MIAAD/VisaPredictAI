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
        assert ops & {refl._REFLECTION_MODULE_ESCAPE, refl._BUILTINS_DYNAMIC_LOOKUP}, f"{label} debe ser un escape (B265): {ops}"  # fmt: skip


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
        assert any("NO REGISTRADA" in p for p in refl.problems()), f"evasion {label} debe fallar (B255)"


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
