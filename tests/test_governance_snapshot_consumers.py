"""B286-B: REDs del gate POSITIVO de consumidores de `tools.governance_snapshot`
(`tools/check_governance_snapshot_consumers.py`). Cada caso viola la biyección exacta (consumidor nuevo/obsoleto, alias,
import de módulo, import dinámico, categoría/kind/operación no declarada, review_by expirado) y el gate lo marca; el árbol
correcto pasa. Complementa las REDs de la observación en `test_governance_snapshot.py` (inventario A-luego-B, cambio de
leaf/modo/uid/nlink, fallo de cierre, `TrackedQuery` forjada/subclasificada) que la migración HEREDA al enrutar los gates
por la snapshot sellada.

En el head previo (`5b73074`) el módulo del gate NO existe → estas pruebas fallan al importar (RED estructural); aquí
pasan."""

from __future__ import annotations

import json
import pathlib

import tools.check_governance_snapshot_consumers as gate


def _entry(**kw):
    base = {"imports": [], "operations": [], "categories": [], "query_kinds": [], "reason": "r", "owner": "o", "review_by": None}  # fmt: skip
    base.update(kw)
    return base


def _setup(monkeypatch, tmp_path, files, consumers, *, schema_version=1, extra_top=None):
    """Monta `files` (rel→fuente) + un registro sintético bajo `tmp_path`, apunta `gate._ROOT` y `gate._git_ls_py` ahí."""
    (tmp_path / "security").mkdir(exist_ok=True)
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    reg = {"schema_version": schema_version, "note": "x", "consumers": consumers}
    if extra_top:
        reg.update(extra_top)
    (tmp_path / "security" / "governance_snapshot_consumers.json").write_text(json.dumps(reg))
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_ls_py", lambda: list(files))


def test_real_registry_is_exact():
    # el árbol real está en biyección exacta con el registro versionado
    assert gate.problems() == [], gate.problems()


def test_registered_consumer_matches(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {
            "tools/x.py": "from tools.governance_snapshot import GovernanceSnapshot\nwith GovernanceSnapshot('/') as s:\n    s.read('a', category='source')\n"
        },  # fmt: skip
        {"tools/x.py": _entry(imports=["GovernanceSnapshot"], operations=["read"], categories=["source"])},
    )
    assert gate.problems() == [], gate.problems()


def test_new_unregistered_consumer_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": "from tools.governance_snapshot import GovernanceSnapshot\n"},
        {},  # registro vacío
    )
    assert any("NO REGISTRADO" in p and "tools/x.py" in p for p in gate.problems()), gate.problems()


def test_obsolete_registry_entry_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": "x = 1\n"},  # ya NO importa
        {"tools/x.py": _entry()},  # pero sigue registrado
    )
    assert any("OBSOLETO" in p for p in gate.problems()), gate.problems()


def test_alias_import_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": "from tools.governance_snapshot import GovernanceSnapshot as GS\n"},
        {"tools/x.py": _entry(imports=["GovernanceSnapshot"])},
    )
    assert any("ALIAS" in p for p in gate.problems()), gate.problems()


def test_module_import_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": "import tools.governance_snapshot\n"},
        {"tools/x.py": _entry()},
    )
    assert any("import de módulo" in p for p in gate.problems()), gate.problems()


def test_dynamic_import_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": "from importlib import import_module\nimport_module('tools.governance_snapshot')\n"},
        {"tools/x.py": _entry()},
    )
    assert any("DINÁMICO" in p for p in gate.problems()), gate.problems()


def test_star_import_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": "from tools.governance_snapshot import *\n"},
        {"tools/x.py": _entry()},
    )
    assert any("import *" in p for p in gate.problems()), gate.problems()


def test_undeclared_category_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {
            "tools/x.py": "from tools.governance_snapshot import GovernanceSnapshot\nwith GovernanceSnapshot('/') as s:\n    s.read('a', category='authority')\n"
        },  # fmt: skip
        {
            "tools/x.py": _entry(imports=["GovernanceSnapshot"], operations=["read"], categories=["source"])
        },  # declara source
    )
    assert any("categories" in p and "biyección" in p for p in gate.problems()), gate.problems()


def test_undeclared_operation_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {
            "tools/x.py": "from tools.governance_snapshot import GovernanceSnapshot, TrackedQuery\nwith GovernanceSnapshot('/') as s:\n    s.tracked(TrackedQuery('suffix', '.py'))\n"
        },  # fmt: skip
        {
            "tools/x.py": _entry(
                imports=["GovernanceSnapshot", "TrackedQuery"], operations=["read"], query_kinds=["suffix"]
            )
        },  # declara read, usa tracked  # fmt: skip
    )
    assert any("operations" in p and "biyección" in p for p in gate.problems()), gate.problems()


def test_undeclared_query_kind_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {
            "tools/x.py": "from tools.governance_snapshot import GovernanceSnapshot, TrackedQuery\nwith GovernanceSnapshot('/') as s:\n    s.tracked(TrackedQuery('prefix', 'tools/'))\n"
        },  # fmt: skip
        {
            "tools/x.py": _entry(
                imports=["GovernanceSnapshot", "TrackedQuery"], operations=["tracked"], query_kinds=["suffix"]
            )
        },  # usa prefix  # fmt: skip
    )
    assert any("query_kinds" in p and "biyección" in p for p in gate.problems()), gate.problems()


def test_ops_only_attributed_to_snapshot_instances(monkeypatch, tmp_path):
    # un `.read()` sobre un objeto NO-snapshot (p. ej. un fichero) NO cuenta como operación de la snapshot — así
    # `governed_import_identity` (que importa utilidades pero no instancia la snapshot) tiene operations=[].
    _setup(
        monkeypatch,
        tmp_path,
        {
            "tools/x.py": "from tools.governance_snapshot import StatSnapshot\n_ = StatSnapshot\npathlib.Path('f').read_text()\n"
        },
        {"tools/x.py": _entry(imports=["StatSnapshot"], operations=[])},
    )
    assert gate.problems() == [], gate.problems()


def test_operation_attributed_via_annotated_param(monkeypatch, tmp_path):
    # un helper que recibe la snapshot como parámetro anotado `GovernanceSnapshot` SÍ atribuye sus operaciones
    src = (
        "from tools.governance_snapshot import GovernanceSnapshot\n"
        "def helper(snap: GovernanceSnapshot) -> bytes:\n"
        "    return snap.read('a', category='source').data\n"
    )
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": src},
        {"tools/x.py": _entry(imports=["GovernanceSnapshot"], operations=["read"], categories=["source"])},
    )
    assert gate.problems() == [], gate.problems()


def test_expired_review_by_fails(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        {"tools/x.py": "from tools.governance_snapshot import GovernanceSnapshot\n"},
        {"tools/x.py": _entry(imports=["GovernanceSnapshot"], review_by="2020-01-01")},
    )
    assert any("EXPIRADO" in p for p in gate.problems()), gate.problems()


def test_bad_schema_version_fails(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, {"tools/x.py": "x = 1\n"}, {}, schema_version=999)
    assert any("schema_version" in p for p in gate.problems()), gate.problems()


def test_duplicate_json_keys_fail(monkeypatch, tmp_path):
    (tmp_path / "security").mkdir(exist_ok=True)
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "x.py").write_text("x = 1\n")
    (tmp_path / "security" / "governance_snapshot_consumers.json").write_text(
        '{"schema_version": 1, "note": "x", "consumers": {}, "consumers": {}}'
    )
    monkeypatch.setattr(gate, "_ROOT", str(tmp_path))
    monkeypatch.setattr(gate, "_git_ls_py", lambda: ["tools/x.py"])
    assert any("duplicad" in p.lower() for p in gate.problems()), gate.problems()


def test_the_four_migrated_gates_use_read_tracked_reverify():
    # wiring: los 4 gates migrados declaran (y por biyección USAN) read+tracked+reverify sobre la snapshot; una regresión
    # a `git ls-files`/`open()` en su `main()` bajaría la operación observada y el gate de consumidores la marcaría.
    reg = json.loads(pathlib.Path("security/governance_snapshot_consumers.json").read_text(encoding="utf-8"))
    for g in (
        "tools/check_commit_frontier.py",
        "tools/check_reflection.py",
        "tools/check_safe_opens.py",
        "tools/check_raw_fs_mutations.py",
    ):
        ops = set(reg["consumers"][g]["operations"])
        assert {"read", "tracked", "reverify"} <= ops, (g, ops)


def test_gate_main_is_green_on_real_tree():
    assert gate.main() == 0
