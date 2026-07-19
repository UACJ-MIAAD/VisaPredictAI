#!/usr/bin/env python
"""B255: gate de REFLEXIÓN por REGISTRO POSITIVO (P0R.5).

En vez de perseguir infinitas variantes de taint sobre aliases de `campaign_bundle` (imposible de cerrar: Python
permite envolver reflexión de formas ilimitadas — `g = [getattr][0]`, wrappers propios, `builtins.__dict__[...]`), este
gate exige que TODA operación de reflexión en el código de PRODUCCIÓN versionado esté DECLARADA en
`security/python_reflection_registry.json`. Cualquier ocurrencia nueva, movida a otra función, o con la llamada
cambiada, FALLA — obligando a registrarla con justificación y revisión. Un wrapper `def reflect(o, n): return
getattr(o, n)` queda registrado por CONTENER `getattr`, aunque su argumento no sea todavía `campaign_bundle`.

Operaciones controladas: getattr/setattr/delattr, vars/globals/locals, `__dict__`/`__getattribute__`,
operator.attrgetter/methodcaller, functools.partial, `__import__`, importlib.import_module, sys.modules, eval/exec/compile.

Además exige un REGISTRO POSITIVO de los módulos autorizados a importar `tools.campaign_bundle`; cualquier importador
nuevo falla.

FRONTERA HONESTA: el gate evita reflexión (o importación de la maquinaria de autoridad) NO REGISTRADA en código
versionado. NO protege contra un mantenedor malicioso que cambie SIMULTÁNEAMENTE el código y esta política — eso es
responsabilidad de la revisión humana y de la rama protegida.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REGISTRY = "security/python_reflection_registry.json"

# Operaciones de reflexión detectadas por REFERENCIA al primitivo (no sólo por llamada): así se cazan `g = [getattr][0]`,
# wrappers propios (`def reflect(o,n): return getattr(o,n)` queda registrado por CONTENER getattr) y aliases por
# contenedor — el registro no persigue el objeto en runtime, sólo exige declarar CADA referencia al primitivo.
_OPS_NAME = frozenset({"getattr", "setattr", "delattr", "vars", "globals", "locals", "eval", "exec", "compile", "__import__", "attrgetter", "methodcaller", "partial", "import_module", "__getattribute__"})  # fmt: skip
_OPS_ATTR = frozenset({"attrgetter", "methodcaller", "import_module", "__getattribute__", "__dict__", "partial"})
# Primitivos que se pueden IMPORTAR por nombre desde su módulo (`from importlib import import_module`,
# `from builtins import getattr as g`, `from sys import modules`): el import mismo es una ocurrencia registrable, así se
# caza el alias antes de usarlo.
_IMPORTABLE = _OPS_NAME | {"import_module", "__getattribute__"}
_CB_MODULE = "campaign_bundle"


def _git_tracked_py() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--", "*.py"], capture_output=True, text=True)
    except OSError:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()] if out.returncode == 0 else []


def _production_files() -> list[str]:
    """Ficheros .py de PRODUCCIÓN versionados: excluye `tests/` (los tests EJERCEN reflexión adversarial a propósito)."""
    return [f for f in _git_tracked_py() if not f.startswith("tests/")]


def _enclosing(fnames: list[tuple[str, int, int]], lineno: int) -> str:
    """Nombre de la función que encierra `lineno` (la más interna); '<module>' si es de nivel de módulo."""
    best = "<module>"
    best_lo = -1
    for name, lo, hi in fnames:
        if lo <= lineno <= hi and lo > best_lo:
            best, best_lo = name, lo
    return best


def _reflection_op(node: ast.AST) -> str | None:
    """Devuelve la operación de reflexión que `node` REFERENCIA, o None. Detecta el PRIMITIVO por referencia —un Name
    (`getattr`, `attrgetter`, `partial`, …) o un Attribute (`operator.attrgetter`, `importlib.import_module`,
    `x.__dict__`, `x.__getattribute__`, `sys.modules`)— NO sólo cuando se llama. Así `g = [getattr][0]`, un wrapper que
    CONTIENE `getattr`, o `builtins.__dict__[n]` quedan cazados. Un literal string ('getattr') es ast.Constant, no un
    Name/Attribute → NO se confunde con una referencia."""
    if isinstance(node, ast.Name) and node.id in _OPS_NAME:
        return node.id
    if isinstance(node, ast.Attribute):
        if node.attr in _OPS_ATTR:
            return node.attr
        if node.attr == "modules" and isinstance(node.value, ast.Name) and node.value.id == "sys":
            return "sys.modules"
    return None


def _entry_id(rel: str, fn: str, op: str) -> str:
    """Clave por (fichero, función que encierra, operación). Referencias IDÉNTICAS del mismo primitivo en la MISMA
    función colapsan (con `count`); mover a OTRA función o a OTRO fichero da OTRO id (ocurrencia nueva → falla)."""
    return hashlib.sha256(f"{rel}::{fn}::{op}".encode()).hexdigest()


def scan_reflection(files: list[str]) -> dict[str, dict]:
    """Escanea `files` y devuelve `{entry_id: {file, function, op, snippet, count}}` por REFERENCIA al primitivo."""
    out: dict[str, dict] = {}
    for rel in files:
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except OSError, SyntaxError:
            continue
        fnames = [
            (n.name, n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        ]
        for node in ast.walk(tree):
            op = _reflection_op(node)
            if op is None and isinstance(node, ast.ImportFrom):
                op = _import_op(node)  # `from importlib import import_module`, `from sys import modules`, alias `as g`
            if op is None:
                continue
            fn = _enclosing(fnames, getattr(node, "lineno", -1))
            eid = _entry_id(rel, fn, op)
            if eid in out:
                out[eid]["count"] += 1
            else:
                try:
                    snippet = ast.unparse(node)
                except ValueError, AttributeError:
                    snippet = op
                out[eid] = {"file": rel, "function": fn, "op": op, "snippet": snippet[:120], "count": 1}
    return out


def _import_op(node: ast.ImportFrom) -> str | None:
    """Devuelve la operación si `node` IMPORTA un primitivo de reflexión por su nombre ORIGINAL (aunque lo aliase con
    `as`): `from builtins import getattr as g`, `from importlib import import_module`, `from sys import modules`."""
    for a in node.names:
        if a.name in _IMPORTABLE:
            return a.name
        if a.name == "modules" and node.module == "sys":
            return "sys.modules"
    return None


def scan_cb_importers(files: list[str]) -> set[str]:
    """Ficheros de producción que IMPORTAN la maquinaria de autoridad `tools.campaign_bundle` (en cualquier forma)."""
    importers: set[str] = set()
    for rel in files:
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except OSError, SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "tools.campaign_bundle" for a in node.names):
                importers.add(rel)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "tools.campaign_bundle" or (node.module in ("tools", None) and any(a.name == _CB_MODULE for a in node.names)):  # fmt: skip
                    importers.add(rel)
    return importers


def _load_registry() -> tuple[dict, list[str]]:
    try:
        with open(os.path.join(ROOT, _REGISTRY), encoding="utf-8") as fh:
            return json.load(fh), []
    except (OSError, ValueError) as exc:
        return {}, [f"{_REGISTRY}: ilegible/no-JSON ({exc}) (fail-closed B255)"]


def problems() -> list[str]:
    """Gate: toda ocurrencia de reflexión de producción debe estar en el registro con su `count`; ningún importador de
    `tools.campaign_bundle` fuera del registro positivo. Ocurrencia nueva/movida/cambiada, count distinto, entrada
    obsoleta del registro, o importador no autorizado → FALLA. Fail-closed ante registro ilegible o git vacío."""
    reg, errs = _load_registry()
    if errs:
        return errs
    files = _production_files()
    if not files:
        return ["git ls-files no devolvió .py de producción (fail-closed B255)"]
    problems: list[str] = []

    observed = scan_reflection(files)
    entries = reg.get("entries")
    if not isinstance(entries, dict):
        return [f"{_REGISTRY}: sin 'entries' (fail-closed B255)"]
    for eid, occ in observed.items():
        want = entries.get(eid)
        if want is None:
            problems.append(f"REFLEXIÓN NO REGISTRADA: {occ['op']} en {occ['file']}::{occ['function']} → `{occ['snippet']}` (registrar en {_REGISTRY} con justificación) (B255)")  # fmt: skip
        elif want.get("count") != occ["count"]:
            problems.append(f"{occ['file']}::{occ['function']}: {occ['op']} aparece {occ['count']}× (registrado {want.get('count')}×) (B255)")  # fmt: skip
    for eid, want in entries.items():
        if eid not in observed:
            problems.append(f"entrada de reflexión OBSOLETA en el registro ({want.get('op')} en {want.get('file')}::{want.get('function')}) — ya no existe en el código (B255)")  # fmt: skip

    observed_imp = scan_cb_importers(files)
    authorized = reg.get("authorized_campaign_bundle_importers")
    if not isinstance(authorized, list):
        return [*problems, f"{_REGISTRY}: sin 'authorized_campaign_bundle_importers' (fail-closed B255)"]
    auth_set = set(authorized)
    for imp in sorted(observed_imp - auth_set):
        problems.append(f"IMPORTADOR NO AUTORIZADO de tools.campaign_bundle: {imp} (registrar en {_REGISTRY}) (B255)")
    for imp in sorted(auth_set - observed_imp):
        problems.append(f"importador autorizado OBSOLETO ({imp}) — ya no importa tools.campaign_bundle (B255)")
    return problems


def main() -> int:
    probs = problems()
    if probs:
        print("✗ registro de reflexión violado:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ toda reflexión de producción está registrada; importadores de campaign_bundle autorizados ({_REGISTRY})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
