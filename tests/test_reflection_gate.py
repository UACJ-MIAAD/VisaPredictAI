"""B255: gate de reflexión por REGISTRO POSITIVO (tools/check_reflection.py). Toda reflexión de producción está
declarada en security/python_reflection_registry.json; cualquier ocurrencia nueva/movida, o un importador nuevo de
tools.campaign_bundle, FALLA. Las siete evasiones que el taint no podía cerrar quedan cazadas por DETECCIÓN de la
referencia al primitivo (no por seguir el objeto en runtime)."""

from __future__ import annotations

import tools.check_reflection as refl


def test_reflection_registry_is_satisfied():
    # el árbol real casa EXACTAMENTE con el registro (0 no-registradas, 0 obsoletas, 0 importadores nuevos)
    assert refl.problems() == []


def test_reflection_gate_catches_the_seven_evasions(tmp_path, monkeypatch):
    # las 7 evasiones que el taint de aliases no podía cerrar: todas REFERENCIAN un primitivo → detectadas → sin
    # registrar en un fichero nuevo → el gate falla.
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
        f = tmp_path / f"{label}.py"
        f.write_text(body)
        monkeypatch.setattr(refl, "_production_files", lambda ff=f: [str(ff)])
        probs = refl.problems()
        assert any("NO REGISTRADA" in p for p in probs), f"evasion {label} debe fallar (B255): {probs[:2]}"


def test_reflection_gate_catches_aliased_and_from_imports(tmp_path, monkeypatch):
    # round 2: importar un primitivo por nombre (aunque se aliase con `as`) es una ocurrencia registrable → un fichero
    # nuevo con `from importlib import import_module` / `from sys import modules` / `from builtins import getattr as g`
    # / `from operator import attrgetter as ag` FALLA.
    cases = {
        "from_importlib": "from importlib import import_module\nimport_module(m)\n",
        "from_sys_modules": "from sys import modules\nmodules[m]\n",
        "getattr_aliased": "from builtins import getattr as g\ng(x, n)\n",
        "attrgetter_aliased": "from operator import attrgetter as ag\nag('x')(o)\n",
    }
    for label, body in cases.items():
        f = tmp_path / f"{label}.py"
        f.write_text(body)
        monkeypatch.setattr(refl, "_production_files", lambda ff=f: [str(ff)])
        assert any("NO REGISTRADA" in p for p in refl.problems()), f"import-evasion {label} debe fallar (B255)"


def test_reflection_gate_detects_each_primitive(tmp_path):
    # scan_reflection DETECTA cada primitivo por referencia (Name o Attribute), no sólo por llamada.
    src = (
        "x = getattr\n"  # Name
        "y = obj.__dict__\n"  # Attribute __dict__
        "z = obj.__getattribute__\n"  # Attribute __getattribute__
        "import operator\n"
        "ag = operator.attrgetter\n"  # Attribute attrgetter
        "import sys\n"
        "m = sys.modules\n"  # sys.modules
    )
    f = tmp_path / "prims.py"
    f.write_text(src)
    ops = {e["op"] for e in refl.scan_reflection([str(f)]).values()}
    assert {"getattr", "__dict__", "__getattribute__", "attrgetter", "sys.modules"} <= ops, ops


def test_reflection_gate_catches_new_cb_importer(tmp_path, monkeypatch):
    # un importador NUEVO de tools.campaign_bundle fuera del registro positivo debe fallar.
    f = tmp_path / "sneaky.py"
    f.write_text("import tools.campaign_bundle as cb\n")
    monkeypatch.setattr(refl, "_production_files", lambda: [str(f)])
    assert any("IMPORTADOR NO AUTORIZADO" in p for p in refl.problems()), "importador nuevo debe fallar (B255)"


def test_reflection_gate_flags_count_drift(tmp_path, monkeypatch):
    # añadir una referencia MÁS del mismo primitivo en la misma función (count mismatch) debe fallar.
    real = refl.problems()
    assert real == []
    # simula: el registro dice count N para una entrada, el código tiene N+1
    import copy

    reg = copy.deepcopy(refl._load_registry()[0])
    # baja el count de la primera entrada a 0 → el código observará >0 → mismatch
    first = next(iter(reg["entries"]))
    reg["entries"][first]["count"] = 0
    monkeypatch.setattr(refl, "_load_registry", lambda: (reg, []))
    assert any("aparece" in p and "registrado" in p for p in refl.problems()), "count drift debe fallar (B255)"


def test_reflection_gate_fail_closed_missing_registry(monkeypatch):
    monkeypatch.setattr(refl, "_REGISTRY", "security/does_not_exist.json")
    assert refl.problems(), "registro ausente debe fallar cerrado (B255)"
