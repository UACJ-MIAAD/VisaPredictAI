#!/usr/bin/env python
"""B286-B: gate POSITIVO de consumidores de `tools.governance_snapshot`.

La observación gobernada (`GovernanceSnapshot`) es la ÚNICA superficie autorizada para leer ficheros/inventario git en la
gobernanza P0R.5. Este gate exige una BIYECCIÓN EXACTA entre `security/governance_snapshot_consumers.json` y el árbol
`.py` de producción versionado (excluye `tests/` y el propio módulo):

- exactamente estos módulos importan `from tools.governance_snapshot import <nombres>` en forma ESTÁTICA — SIN alias
  (`… as X`), SIN `import tools.governance_snapshot` (import de módulo), SIN import DINÁMICO
  (`__import__`/`import_module("tools.governance_snapshot")`), SIN `import *`;
- los nombres importados por cada módulo == `imports`;
- las categorías (`category="…"`) y query-kinds (`TrackedQuery("…", …)`) usadas == las declaradas;
- las operaciones (`read`/`tracked`/`reverify`/`head_commit`) invocadas sobre una instancia `GovernanceSnapshot` (variable
  ligada al constructor, cadena directa, o parámetro anotado `GovernanceSnapshot`) == `operations`;
- `reason`/`owner` presentes y no vacíos; `review_by` (excepción temporal) no expirado.

Un consumidor NUEVO, un alias, un import dinámico, una categoría/kind/operación no declarada, o una entrada OBSOLETA →
PROBLEMA (fail-closed). Sólo stdlib (`ast`/`json`/`os`/`subprocess`/`datetime`); NO importa `governance_snapshot` (evita
la auto-referencia) — lo observa como texto versionado, como cualquier otro consumidor."""

from __future__ import annotations

import ast
import datetime
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REGISTRY = "security/governance_snapshot_consumers.json"
_MODULE = "tools.governance_snapshot"
_MODULE_FILE = "tools/governance_snapshot.py"
_SELF = "tools/check_governance_snapshot_consumers.py"
_SCHEMA_VERSION = 1
_TOP_KEYS = {"schema_version", "note", "consumers"}
_ENTRY_KEYS = {"imports", "operations", "categories", "query_kinds", "reason", "owner", "review_by"}
_OPS = frozenset({"read", "tracked", "reverify", "head_commit"})
_CATEGORIES = frozenset({"contract", "authority", "source"})
_QUERY_KINDS = frozenset({"prefix", "suffix", "exact"})


def _git_ls_py() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--", "*.py"], capture_output=True, text=True)
    except OSError:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()] if out.returncode == 0 else []


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def _is_gs_ctor(node: ast.AST) -> bool:
    """`GovernanceSnapshot(...)` como Name directo o `<algo>.GovernanceSnapshot(...)`."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "GovernanceSnapshot") or (
        isinstance(f, ast.Attribute) and f.attr == "GovernanceSnapshot"
    )


def _snapshot_instance_vars(tree: ast.AST) -> set[str]:
    """Nombres que refieren a una INSTANCIA `GovernanceSnapshot`: ligados por `with GovernanceSnapshot(...) as X`,
    `X = GovernanceSnapshot(...)`, o parámetros de función anotados con `GovernanceSnapshot` (cubre los helpers que reciben
    la snapshot como argumento). Coarse (no-scoped) a propósito — sólo alimenta la atribución de operaciones."""
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.With):
            for item in n.items:
                if _is_gs_ctor(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
        elif isinstance(n, ast.Assign) and _is_gs_ctor(n.value):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = n.args
            for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                if a.annotation is not None and "GovernanceSnapshot" in ast.unparse(a.annotation):
                    names.add(a.arg)
    return names


def _analyze(rel: str, tree: ast.AST) -> tuple[set[str] | None, list[str], set[str], set[str], set[str]]:
    """Devuelve `(nombres_importados|None, problemas_forma, categorías, query_kinds, operaciones)`. `None` = el módulo NO
    importa de `governance_snapshot`. `problemas_forma` cubre alias/import-de-módulo/import-dinámico/`import *`."""
    imported: set[str] | None = None
    form_problems: list[str] = []
    cats: set[str] = set()
    kinds: set[str] = set()
    ops: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == _MODULE:
            imported = set() if imported is None else imported
            for a in n.names:
                if a.name == "*":
                    form_problems.append(f"{rel}: `from {_MODULE} import *` prohibido (biyección imposible) (B286-B)")
                elif a.asname is not None:
                    form_problems.append(f"{rel}: import con ALIAS `{a.name} as {a.asname}` de {_MODULE} prohibido (B286-B)")  # fmt: skip
                else:
                    imported.add(a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == _MODULE:
                    imported = set() if imported is None else imported
                    form_problems.append(f"{rel}: `import {_MODULE}`{' as ' + a.asname if a.asname else ''} (import de módulo) prohibido — usar `from {_MODULE} import <nombre>` (B286-B)")  # fmt: skip
        elif isinstance(n, ast.Call):
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if fname in ("__import__", "import_module") and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == _MODULE:  # fmt: skip
                imported = set() if imported is None else imported
                form_problems.append(f"{rel}: import DINÁMICO de {_MODULE} ({fname}) prohibido (B286-B)")
            for kw in n.keywords:
                if kw.arg == "category" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    cats.add(kw.value.value)
            if isinstance(f, ast.Name) and f.id == "TrackedQuery" and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):  # fmt: skip
                kinds.add(n.args[0].value)
    if imported is None:
        # aun sin ImportFrom, un import dinámico/alias registrado arriba sí marca al módulo como consumidor (forma inválida)
        return (None if not form_problems else set()), form_problems, cats, kinds, ops
    snap_vars = _snapshot_instance_vars(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in _OPS:
            base = n.value
            if (isinstance(base, ast.Name) and base.id in snap_vars) or _is_gs_ctor(base):
                ops.add(n.attr)
    return imported, form_problems, cats, kinds, ops


def _entry_problems(rel: str, entry: object, observed: tuple[set[str], set[str], set[str], set[str]]) -> list[str]:
    imported, cats, kinds, ops = observed
    problems: list[str] = []
    if not (isinstance(entry, dict) and set(entry) == _ENTRY_KEYS):
        return [f"{_REGISTRY}[{rel}]: claves de entrada != {sorted(_ENTRY_KEYS)} (fail-closed)"]
    checks = (
        ("imports", imported, None),
        ("operations", ops, _OPS),
        ("categories", cats, _CATEGORIES),
        ("query_kinds", kinds, _QUERY_KINDS),
    )
    for field, obs, universe in checks:
        decl = entry[field]
        if not (isinstance(decl, list) and all(isinstance(x, str) for x in decl)):
            problems.append(f"{_REGISTRY}[{rel}]: {field} no es lista de str")
            continue
        if len(decl) != len(set(decl)):
            problems.append(f"{_REGISTRY}[{rel}]: {field} tiene duplicados")
        decl_set = set(decl)
        if universe is not None and not decl_set <= universe:
            problems.append(f"{_REGISTRY}[{rel}]: {field} {sorted(decl_set - universe)} fuera de {sorted(universe)}")
        if decl_set != obs:
            problems.append(f"{_REGISTRY}[{rel}]: {field} declarado {sorted(decl_set)} != observado {sorted(obs)} (biyección) (B286-B)")  # fmt: skip
    for field in ("reason", "owner"):
        if not (isinstance(entry[field], str) and entry[field].strip()):
            problems.append(f"{_REGISTRY}[{rel}]: {field} vacío")
    rb = entry["review_by"]
    if rb is not None:
        try:
            rb_date = datetime.date.fromisoformat(rb) if isinstance(rb, str) else None
        except ValueError:
            rb_date = None
        if rb_date is None:
            problems.append(f"{_REGISTRY}[{rel}]: review_by no es null ni fecha ISO")
        elif rb_date < datetime.date.today():
            problems.append(f"{_REGISTRY}[{rel}]: review_by {rb} EXPIRADO — re-revisar el consumidor (B286-B)")
    return problems


def problems() -> list[str]:
    try:
        with open(os.path.join(_ROOT, _REGISTRY), encoding="utf-8") as fh:
            reg = json.loads(fh.read(), object_pairs_hook=_no_dup_pairs)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [f"{_REGISTRY}: ilegible/no-JSON/duplicado ({exc}) (fail-closed B286-B)"]
    if not isinstance(reg, dict) or set(reg) != _TOP_KEYS:
        return [f"{_REGISTRY}: claves superiores != {sorted(_TOP_KEYS)} (fail-closed)"]
    if not (type(reg["schema_version"]) is int and reg["schema_version"] == _SCHEMA_VERSION):
        return [f"{_REGISTRY}: schema_version no es {_SCHEMA_VERSION}"]
    consumers = reg["consumers"]
    if not isinstance(consumers, dict):
        return [f"{_REGISTRY}: 'consumers' no es un objeto"]

    files = _git_ls_py()
    if not files:
        return ["git ls-files no devolvió .py (fail-closed B286-B)"]
    observed: dict[str, tuple[set[str], set[str], set[str], set[str]]] = {}
    problems: list[str] = []
    for rel in files:
        if rel in (_MODULE_FILE, _SELF) or rel.startswith("tests/"):
            continue
        try:
            with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            problems.append(f"{rel}: ilegible/no parseable ({exc}) (fail-closed B286-B)")
            continue
        imported, form_probs, cats, kinds, ops = _analyze(rel, tree)
        problems.extend(form_probs)
        if imported is not None:
            observed[rel] = (imported, cats, kinds, ops)

    obs_set, reg_set = set(observed), set(consumers)
    for rel in sorted(obs_set - reg_set):
        problems.append(f"CONSUMIDOR NO REGISTRADO de {_MODULE}: {rel} (registrar en {_REGISTRY}) (B286-B)")
    for rel in sorted(reg_set - obs_set):
        problems.append(f"consumidor REGISTRADO OBSOLETO ({rel}) — ya no importa {_MODULE} (B286-B)")
    for rel in sorted(obs_set & reg_set):
        problems.extend(_entry_problems(rel, consumers[rel], observed[rel]))
    return problems


def main() -> int:
    probs = problems()
    if probs:
        print("✗ registro de consumidores de governance_snapshot violado:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ biyección exacta de consumidores de {_MODULE} ({_REGISTRY})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
