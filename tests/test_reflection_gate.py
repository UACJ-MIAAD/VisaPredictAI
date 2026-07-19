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
