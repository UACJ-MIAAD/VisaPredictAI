#!/usr/bin/env python
"""B255/B259/B260: gate de REFLEXIÓN por REGISTRO POSITIVO con identidad SEMÁNTICA (P0R.5).

Perseguir aliases de `campaign_bundle` por taint es un arms-race infinito. En su lugar, TODA operación de reflexión en
el Python de PRODUCCIÓN versionado debe estar DECLARADA en `security/python_reflection_registry.json`. Diferencias
frente a la v1 (que era defraudable/fail-open):

- **Identidad semántica (B259):** el ID de cada ocurrencia deriva de `sha256` de JSON canónico de
  `{file, qualname, op, statement_ast_sha256, occurrence_index}` — NO de `file::function::op`. Cambiar el objeto, el
  nombre o la forma de la llamada cambia el AST del statement mínimo que la contiene → cambia el ID → exige revisión.
  El número de línea NO es identidad (sólo mensaje humano).
- **Cobertura y fail-closed (B260):** tabla de símbolos AST que resuelve `import builtins as b` / `sys as s` /
  `importlib as il` / `operator`/`functools`, y `from builtins import getattr as g` / `from operator import attrgetter
  as ag` / `from functools import partial as p` / `from importlib import import_module as im`, con aliases transitivos;
  qualname completo con `FunctionDef`/`AsyncFunctionDef`/métodos/`<lambda>`/anidadas; imports relativos resueltos por
  la ruta del fichero y `node.level`. Sintaxis inválida, error de lectura/UTF-8, JSON duplicado, schema desconocido,
  entrada obsoleta/nueva, metadatos divergentes o `review_by` expirado → PROBLEMA ESTRUCTURADO (nunca `continue`
  silencioso ni traceback).

Operaciones controladas: getattr/setattr/delattr, vars/globals/locals, `__dict__`/`__getattribute__`,
operator.attrgetter/methodcaller, functools.partial, `__import__`, importlib.import_module, sys.modules, eval/exec/compile.

FRONTERA HONESTA: evita reflexión (o importación de la maquinaria de autoridad) NO REGISTRADA en código versionado. NO
protege contra un mantenedor que cambie a la vez código y política — eso es revisión humana + rama protegida.
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import subprocess
import sys
from typing import NamedTuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REGISTRY = "security/python_reflection_registry.json"
_SCHEMA_VERSION = 2
_SCANNER_VERSION = 16  # B265/…/B321/B324: además, ADQUIRIR el módulo-fábrica raíz (`import builtins`/`import importlib[.x]` sin alias de submódulo) o `sys.modules[factory]` es PROHIBIDO; los submódulos con alias explícito se permiten
_REGISTRY_TOP_KEYS = {
    "schema_version",
    "scanner_version",
    "note",
    "operations_controlled",
    "authorized_campaign_bundle_importers",
    "entries",  # fmt: skip
}
_ENTRY_KEYS = {"file", "qualname", "op", "statement_ast_sha256", "occurrence_index", "justification", "review_by"}

# Primitivos builtin (Name directo o `builtins.<name>`).
_BUILTIN_PRIMS = frozenset({"getattr", "setattr", "delattr", "vars", "globals", "locals", "eval", "exec", "compile", "__import__"})  # fmt: skip
# Primitivos que viven en un módulo concreto (`operator.attrgetter`, `functools.partial`, `importlib.import_module`).
_MODULE_PRIMS = {"attrgetter": "operator", "methodcaller": "operator", "partial": "functools", "import_module": "importlib"}  # fmt: skip
# Atributos de reflexión sobre CUALQUIER objeto.
_ATTR_PRIMS = frozenset({"__dict__", "__getattribute__"})
_CANONICAL_MODULES = frozenset({"builtins", "sys", "importlib", "operator", "functools"})
_IMPORTABLE_PRIMS = _BUILTIN_PRIMS | frozenset(_MODULE_PRIMS)
# B265: dos operaciones conservadoras. `reflection-module-escape` = un objeto módulo canónico se FUGA a un contenedor/
# atributo/subscript/argumento/retorno/construcción no comprendida → ya no se puede seguir su reflexión, así que es una
# ocurrencia. `builtins.dynamic-lookup` = subscript sobre builtins/__builtins__ con clave NO constante (op final
# desconocida). Convertir toda fuga conocida/ambigua en una ocurrencia; no se afirma resolver semántica Python arbitraria.
_REFLECTION_MODULE_ESCAPE = "reflection-module-escape"
_BUILTINS_DYNAMIC_LOOKUP = "builtins.dynamic-lookup"
# B285: política POSITIVA — TODA llamada rooteada en un módulo canónico produce una ocurrencia registrable (no una lista
# de terminales que deja invisibles a SourceFileLoader.set_data / sys.meta_path.insert / sys.path_hooks.append / …).
_CANONICAL_ROOTED_CALL = "canonical-rooted-call"
# B308/B316: DENY-BY-DEFAULT — el resultado de una fábrica DINÁMICA (`__import__`/`import_module`, bare o aliased) está
# globalmente PROHIBIDO (no registrable) en CUALQUIER posición. Ya NO hay patrón seguro: `deep_smoke` importa su stack de
# forma estática, así que producción no requiere ninguna llamada a una fábrica de import dinámico.
_DYNAMIC_MODULE_RESULT_ESCAPE = "dynamic-module-result-escape"
# B310/B317: contrato SINTÁCTICO POSITIVO — OBTENER una fábrica de import dinámico está PROHIBIDO en el ORIGEN, aunque
# nunca se llame: `__import__`/`__builtins__` en Load, `from builtins|importlib import __import__|import_module`,
# `<builtins|importlib>.__import__/.import_module` como Attribute, y todo lookup dinámico (getattr/vars/__dict__/
# attrgetter/methodcaller/partial) sobre builtins/importlib con nombre LITERAL o CALCULADO.
_DYNAMIC_IMPORT_FACTORY_VALUE = "dynamic-import-factory-value"
OPERATIONS_CONTROLLED = tuple(sorted(_BUILTIN_PRIMS | frozenset(_MODULE_PRIMS) | _ATTR_PRIMS | {"__dict__", "sys.modules", _REFLECTION_MODULE_ESCAPE, _BUILTINS_DYNAMIC_LOOKUP, _CANONICAL_ROOTED_CALL, _DYNAMIC_MODULE_RESULT_ESCAPE, _DYNAMIC_IMPORT_FACTORY_VALUE}))  # fmt: skip
_CB_MODULE = "tools.campaign_bundle"
# B265: en los módulos de AUTORIDAD, un escape/lookup dinámico está PROHIBIDO (no registrable).
_AUTHORITY_MODULES = frozenset({"tools/campaign_bundle.py", "tools/merge_campaign_pools.py", "tools/governed_fs.py", "tools/governed_read.py"})  # fmt: skip


def _git_tracked_py() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--", "*.py"], capture_output=True, text=True)
    except OSError:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()] if out.returncode == 0 else []


def _production_files() -> list[str]:
    """Ficheros .py de PRODUCCIÓN versionados: excluye `tests/` (los tests EJERCEN reflexión adversarial a propósito)."""
    return [f for f in _git_tracked_py() if not f.startswith("tests/")]


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """`import sys as s` → {s: sys}; `import builtins` → {builtins: builtins}; `__builtins__` (implícito) → builtins;
    y cadenas transitivas `b2 = builtins` por FIXPOINT. Sólo módulos canónicos de reflexión."""
    out: dict[str, str] = {"__builtins__": "builtins"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in _CANONICAL_MODULES:
                    out[a.asname or a.name] = a.name
    changed = True
    while changed:
        changed = False
        for tgt, val in _name_bindings(tree):
            if tgt in out:
                continue
            if isinstance(val, ast.Name) and val.id in out:
                out[tgt] = out[val.id]
                changed = True
    return out


def _iter_targets(target: ast.AST, value: ast.AST):
    """B297: empareja `(nombre, valor)` incluyendo asignación MÚLTIPLE y destructuring de tuple/list del MISMO largo
    (`a = b = X` liga a ambos; `(m, n) = (X, Y)` liga m→X, n→Y; anidado). Un destructuring de largo desigual no se sigue."""
    if isinstance(target, ast.Name):
        yield target.id, value
    elif (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        for t, v in zip(target.elts, value.elts, strict=True):  # largo verificado arriba
            yield from _iter_targets(t, v)


def _name_bindings(tree: ast.AST):
    """Itera `(nombre, valor)` sobre Assign (uno o VARIOS targets, con destructuring)/AnnAssign/NamedExpr — base de los
    fixpoints de alias (B297: ya no sólo un Name simple)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                yield from _iter_targets(tgt, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            yield node.target.id, node.value
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            yield node.target.id, node.value


def _prim_aliases(tree: ast.AST) -> dict[str, str]:
    """Nombres locales ligados a un primitivo por `from … import … [as …]` o por asignación transitiva (`g = getattr`,
    `h = g`), por FIXPOINT. Devuelve {nombre_local: op}."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in _IMPORTABLE_PRIMS:
                    out[a.asname or a.name] = a.name
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            tgt = val = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                tgt, val = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                tgt, val = node.target.id, node.value
            elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
                tgt, val = node.target.id, node.value
            if tgt is None or tgt in out:
                continue
            if isinstance(val, ast.Name) and (val.id in _BUILTIN_PRIMS or val.id in out):
                out[tgt] = out.get(val.id, val.id)
                changed = True
    return out


def _resolve_op(node: ast.AST, mod_aliases: dict[str, str], prim_aliases: dict[str, str]) -> str | None:
    """Devuelve la operación de reflexión que `node` REFERENCIA, o None. Cubre Name directo/aliased, `builtins.getattr`,
    `operator.attrgetter`/`functools.partial`/`importlib.import_module` (por atributo, con o sin alias de módulo),
    `x.__dict__`/`x.__getattribute__`, y `sys.modules` (con alias de `sys`)."""
    if isinstance(node, ast.Name):
        if node.id in _BUILTIN_PRIMS:
            return node.id
        if node.id in prim_aliases:
            return prim_aliases[node.id]
        return None
    if isinstance(node, ast.Attribute):
        attr = node.attr
        if attr in _ATTR_PRIMS:
            return attr
        if attr in _MODULE_PRIMS:  # attrgetter/methodcaller/partial/import_module — nombres de reflexión inequívocos
            return attr
        recv = node.value
        recv_mod = mod_aliases.get(recv.id) if isinstance(recv, ast.Name) else None
        if attr in _BUILTIN_PRIMS and recv_mod == "builtins":
            return attr
        if attr == "modules" and recv_mod == "sys":
            return "sys.modules"
    if isinstance(node, ast.Subscript):  # `__builtins__['getattr']` / `__builtins__[dyn]`
        base = node.value
        base_mod = mod_aliases.get(base.id) if isinstance(base, ast.Name) else None
        if base_mod == "builtins":
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                return node.slice.value if node.slice.value in _BUILTIN_PRIMS else None
            return _BUILTINS_DYNAMIC_LOOKUP  # clave NO constante → op final desconocida (B265)
    return None


def _qualnames(tree: ast.AST) -> dict[int, str]:
    """id(nodo) → qualname del scope que lo ENCIERRA (FunctionDef/AsyncFunctionDef/ClassDef/<lambda>); '' = módulo."""
    qn: dict[int, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            qn[id(child)] = prefix
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, f"{prefix}.{child.name}" if prefix else child.name)
            elif isinstance(child, ast.Lambda):
                visit(child, f"{prefix}.<lambda>" if prefix else "<lambda>")
            else:
                visit(child, prefix)

    visit(tree, "")
    return qn


def _enclosing_stmts(tree: ast.AST) -> dict[int, ast.stmt]:
    """id(nodo) → el ast.stmt mínimo que lo contiene."""
    out: dict[int, ast.stmt] = {}

    def visit(node: ast.AST, stmt: ast.stmt | None) -> None:
        for child in ast.iter_child_nodes(node):
            cur = child if isinstance(child, ast.stmt) else stmt
            if cur is not None:
                out[id(child)] = cur
            visit(child, cur)

    visit(tree, None)
    return out


def _norm_stmt_sha(stmt: ast.stmt) -> str:
    return hashlib.sha256(ast.dump(stmt, annotate_fields=True, include_attributes=False).encode()).hexdigest()


def _occurrence_id(file: str, qualname: str, op: str, stmt_sha: str, index: int) -> str:
    payload = json.dumps(
        {"file": file, "qualname": qualname, "op": op, "statement_ast_sha256": stmt_sha, "occurrence_index": index},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _escape_op(node: ast.AST, parents: dict[int, ast.AST], mod_aliases: dict[str, str]) -> str | None:
    """B265: un Name de módulo canónico (o `__builtins__`) usado en Load que ESCAPA del seguimiento — dentro de un
    contenedor (List/Tuple/Set/Dict), pasado como argumento, retornado, comparado, o en cualquier construcción que no
    sea `alias.attr` / `alias[...]` / `x = alias` (target Name simple, seguido por el fixpoint) — devuelve
    `reflection-module-escape`. Conservador: si el módulo puede irse a donde no lo seguimos, es una ocurrencia."""
    if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in mod_aliases):
        return None
    parent = parents.get(id(node))
    if parent is None:
        return None
    if isinstance(parent, ast.Attribute) and parent.value is node:
        return None  # `alias.attr` — acceso resuelto por _resolve_op
    if isinstance(parent, ast.Subscript) and parent.value is node:
        return None  # `alias[...]` — subscript resuelto por _resolve_op
    if isinstance(parent, ast.Call) and parent.func is node:
        return None  # B294: `alias(...)` / `miembro(...)` como CALLEE — es una llamada (la cubre _canonical_rooted_call), no un escape; un módulo/miembro pasado como ARGUMENTO (func is not node) sí escapa
    if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name) and parent.value is node:  # fmt: skip
        return None  # `x = alias` — alias simple, seguido por el fixpoint
    if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name) and parent.value is node:
        return None
    if isinstance(parent, ast.NamedExpr) and isinstance(parent.target, ast.Name) and parent.value is node:
        return None
    return _REFLECTION_MODULE_ESCAPE


# Nombres de MAQUINARIA de módulo que pueden RE-PRODUCIR/reflejar un módulo por cadena (no `version`/`argv`/`path`…).
_CHAIN_DANGER_NAMES = frozenset({"load_module", "import_module", "find_module", "find_spec", "find_loader", "exec_module", "create_module", "reload", "get_data", "module_from_spec", "spec_from_file_location", "spec_from_loader"})  # fmt: skip
# B276: terminales reflexivos que, LLAMADOS al final de una cadena peligrosa, tienen EFECTO aunque el resultado se
# descarte (cargar/importar/mutar). Un `builtins.__spec__.loader.load_module('builtins')` como statement descartado ya
# ejecutó el side-effect; no basta con marcar sólo cuando el valor escapa.
_CHAIN_CALL_DANGER = _CHAIN_DANGER_NAMES | frozenset({"setattr", "delattr", "getattr", "exec", "eval", "__import__"})


def _rooted_chain_escape(node: ast.AST, parents: dict[int, ast.AST], mod_aliases: dict[str, str]) -> str | None:
    """B270/B276: una CADENA con raíz en un módulo canónico que accede a MAQUINARIA de módulo (un atributo DUNDER
    `__spec__`/`__loader__`/… o un método loader `load_module`/`import_module`/…), no resuelve a una operación modelada,
    y (a) cuyo RESULTADO ESCAPA (asignado/retornado/pasado) O (b) que EJECUTA una llamada de EFECTO a través de esa
    maquinaria (aunque el resultado se descarte) → `reflection-module-escape`. Ej. escape:
    `b = builtins.__spec__.loader.load_module('builtins')`; ej. efecto descartado:
    `builtins.__spec__.loader.load_module('builtins')` como statement. `sys.version.split()[0]`, `sys.exit(1)`,
    `x = sys.argv`, `sys.stderr.write(...)` (atributos de DATOS, no maquinaria) NO se marcan — evita los `sys.*`
    legítimos; un `sys.__dict__.get(...)` descartado tampoco (la llamada NO es sobre un nombre de maquinaria/terminal)."""
    if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in mod_aliases):
        return None
    cur: ast.AST = node
    resolved_modeled = False
    dangerous = False
    side_effect_call = False
    while True:
        parent = parents.get(id(cur))
        if isinstance(parent, ast.Attribute) and parent.value is cur:
            if _resolve_op(parent, mod_aliases, {}) is not None:
                resolved_modeled = True
            elif (parent.attr.startswith("__") and parent.attr.endswith("__")) or parent.attr in _CHAIN_DANGER_NAMES:
                dangerous = True
            cur = parent
        elif isinstance(parent, ast.Subscript) and parent.value is cur:
            resolved_modeled = resolved_modeled or _resolve_op(parent, mod_aliases, {}) is not None
            cur = parent
        elif isinstance(parent, ast.Call) and parent.func is cur:
            # B276: una llamada a través de la maquinaria peligrosa (callee = método loader o terminal reflexivo) tiene
            # EFECTO aunque el resultado se descarte.
            if dangerous and isinstance(cur, ast.Attribute) and cur.attr in _CHAIN_CALL_DANGER:
                side_effect_call = True
            cur = parent
        else:
            break
    if resolved_modeled or not dangerous:
        return None  # op modelada en la cadena, o cadena de DATOS (sin maquinaria de módulo) → no-escape
    top_parent = parents.get(id(cur))
    escapes = not (top_parent is None or isinstance(top_parent, ast.Expr))  # asignado/retornado/pasado (no descartado)
    if escapes or side_effect_call:  # B276: escape del valor O efecto lateral de la llamada
        return _REFLECTION_MODULE_ESCAPE
    return None


# B289: fábricas canónicas que DEVUELVEN un módulo; un binding `m = import_module(...)`/`__import__(...)` queda rooteado.
_CANONICAL_FACTORY_NAMES = frozenset({"import_module", "__import__"})
# B316/B317: módulos que EXPONEN las fábricas estándar de import dinámico; todo lookup dinámico sobre ellos está prohibido.
_FACTORY_MODULE_ROOTS = frozenset({"builtins", "importlib"})


class _Provenance(NamedTuple):
    rooted: dict[str, str]  # nombre → módulo canónico raíz (módulos/submódulos/miembros/resultados de fábrica)
    sysmod: frozenset[str]  # nombres que aliasean `sys.modules`
    dictnames: frozenset[str]  # nombres que aliasean un `<canónico>.__dict__`
    members: frozenset[
        str
    ]  # B294: nombres traídos por `from <canónico> import <miembro>` (su captura no-call se marca)
    factories: dict[
        str, str
    ]  # B297: nombre → raíz canónica de una FÁBRICA (`__import__`/`import_module`) aún sin llamar
    prim_aliases: dict[str, str]  # B321: nombre → primitivo de ACCESO ('getattr'/'attrgetter'/'partial'/…) por alias


# B297: dominio abstracto de una expresión — 'value' evalúa a un módulo/miembro canónico (o RESULTADO de fábrica);
# 'factory' evalúa a una fábrica aún sin llamar; 'none' no es canónico. Cota de recursión para árboles patológicos.
_EXPR_DEPTH_CAP = 150  # B300: cota alta (nesting realista 61/65/100 se analiza); superarla es fail-closed, no `none`


class _ProvenanceLimitError(Exception):
    """B300: el análisis de procedencia superó su cota (profundidad/nodos). NO se degrada a `none` — `scan_reflection`
    lo convierte en un PROBLEMA fail-closed."""


def _expr_provenance(node: ast.AST, rooted: dict[str, str], factories: dict[str, str], depth: int = 0) -> tuple[str, str | None]:  # fmt: skip
    """B297: clasifica CUALQUIER expresión propagando por Name/Call(fábrica)/Attribute/NamedExpr/IfExp/BoolOp y subscript
    de contenedor literal conocido. Una llamada a fábrica produce `value` AUNQUE aparezca dentro de otra expresión
    (`__import__('os').system(...)`, `(im('os'),)[0]`, `m if c else n`). B300: superar la cota de profundidad LEVANTA
    `_ProvenanceLimitError` (fail-closed), nunca devuelve `none` por pérdida de precisión."""
    if depth > _EXPR_DEPTH_CAP:
        raise _ProvenanceLimitError(f"profundidad > {_EXPR_DEPTH_CAP} (B300)")
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id in factories:
            return ("factory", factories[node.id])
        if node.id in rooted:
            return ("value", rooted[node.id])
        return ("none", None)
    if isinstance(node, ast.Call):  # B308: un RESULTADO DINÁMICO de fábrica NO se rootea (lo prohíbe
        return ("none", None)  # `_dynamic_factory_escape` en su sitio); el lattice sólo cubre imports ESTÁTICOS
    if isinstance(node, ast.Attribute):  # atributo/submódulo de un valor canónico sigue siendo canónico
        k, r = _expr_provenance(node.value, rooted, factories, depth + 1)
        return ("value", r) if k in ("value", "factory") else ("none", None)
    if isinstance(node, ast.NamedExpr):  # walrus: la procedencia es la del valor
        return _expr_provenance(node.value, rooted, factories, depth + 1)
    if isinstance(
        node, (ast.IfExp, ast.BoolOp)
    ):  # conservador: si ALGUNA rama/operando es canónico, el resultado puede serlo
        branches = [node.body, node.orelse] if isinstance(node, ast.IfExp) else list(node.values)
        for b in branches:
            k, r = _expr_provenance(b, rooted, factories, depth + 1)
            if k in ("value", "factory"):
                return (k, r)
        return ("none", None)
    if isinstance(node, ast.Subscript):  # subscript de un contenedor LITERAL con índice/clave constante → el elemento
        container, idx = node.value, node.slice
        if isinstance(container, (ast.Tuple, ast.List)) and isinstance(idx, ast.Constant) and isinstance(idx.value, int):  # fmt: skip
            if -len(container.elts) <= idx.value < len(container.elts):
                return _expr_provenance(container.elts[idx.value], rooted, factories, depth + 1)
        if isinstance(container, ast.Dict) and isinstance(idx, ast.Constant):
            for k2, v2 in zip(container.keys, container.values, strict=True):  # keys/values de ast.Dict son paralelos
                if isinstance(k2, ast.Constant) and k2.value == idx.value:
                    return _expr_provenance(v2, rooted, factories, depth + 1)
        return ("none", None)
    return ("none", None)


def _factory_root(fn: ast.AST, factories: dict[str, str], rooted: dict[str, str]) -> str | None:
    """B294: raíz canónica si `fn` es una FÁBRICA (`__import__`/`import_module`) — bare, aliased o `<canónico>.import_module`.
    Devuelve la raíz del módulo importado (para marcar su RESULTADO como rooteado)."""
    if isinstance(fn, ast.Name) and fn.id in factories:
        return factories[fn.id]
    if isinstance(fn, ast.Attribute) and fn.attr in _CANONICAL_FACTORY_NAMES:
        cur: ast.AST = fn.value
        while isinstance(cur, (ast.Attribute, ast.Subscript)):
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id in rooted:
            return rooted[cur.id]
    return None


def _factory_module_root(node: ast.AST, prov: _Provenance) -> str | None:
    """B316/B317: raíz ∈ {builtins, importlib} si `node` evalúa a ESE módulo canónico (o submódulo/atributo suyo). Son
    los módulos que EXPONEN las fábricas estándar de import dinámico (`builtins.__import__`, `importlib.import_module`);
    todo lookup dinámico sobre ellos está prohibido. `_ProvenanceLimitError` propaga (fail-closed en `scan_reflection`)."""
    kind, root = _expr_provenance(node, prov.rooted, prov.factories, 0)
    return root if kind == "value" and root in _FACTORY_MODULE_ROOTS else None


# B321: primitivos de ACCESO dinámico (recuperan un atributo por nombre); su alias sobre builtins/importlib recupera la
# fábrica y es tan peligroso como la forma directa.
_ACCESSOR_PRIMS = frozenset({"getattr", "vars", "attrgetter", "methodcaller", "partial"})


def _accessor_of_expr(node: ast.AST, rooted: dict[str, str], acc: dict[str, str], depth: int = 0) -> str | None:
    """B321: primitivo de acceso al que resuelve `node`, propagando por la MISMA procedencia semántica que los módulos
    (Name/Attribute/NamedExpr/IfExp/BoolOp/subscript de contenedor literal). `IfExp`/`BoolOp` sólo resuelve si TODAS las
    ramas dan el mismo primitivo. Superar la cota LEVANTA `_ProvenanceLimitError` (fail-closed, nunca `none`)."""
    if depth > _EXPR_DEPTH_CAP:
        raise _ProvenanceLimitError(f"profundidad > {_EXPR_DEPTH_CAP} (B321)")
    if isinstance(node, ast.Name):
        if node.id in _BUILTIN_PRIMS and node.id in _ACCESSOR_PRIMS:  # `getattr`/`vars` builtin bare
            return node.id
        return acc.get(node.id)  # alias (transitivo/from-import/contenedor)
    if isinstance(node, ast.Attribute) and node.attr in _ACCESSOR_PRIMS and node.attr in _MODULE_PRIMS:
        cur: ast.AST = node.value  # `operator.attrgetter`/`functools.partial` (con o sin alias de módulo)
        while isinstance(cur, (ast.Attribute, ast.Subscript)):
            cur = cur.value
        if isinstance(cur, ast.Name) and rooted.get(cur.id) == _MODULE_PRIMS[node.attr]:
            return node.attr
        return None
    if isinstance(node, ast.NamedExpr):
        return _accessor_of_expr(node.value, rooted, acc, depth + 1)
    if isinstance(node, (ast.IfExp, ast.BoolOp)):  # TODAS las ramas deben dar el MISMO primitivo
        branches = [node.body, node.orelse] if isinstance(node, ast.IfExp) else list(node.values)
        prims = {_accessor_of_expr(b, rooted, acc, depth + 1) for b in branches}
        return prims.pop() if len(prims) == 1 and None not in prims else None
    if isinstance(node, ast.Subscript):  # subscript de contenedor LITERAL con índice/clave constante → el elemento
        container, idx = node.value, node.slice
        if isinstance(container, (ast.Tuple, ast.List)) and isinstance(idx, ast.Constant) and isinstance(idx.value, int):  # fmt: skip
            if -len(container.elts) <= idx.value < len(container.elts):
                return _accessor_of_expr(container.elts[idx.value], rooted, acc, depth + 1)
        if isinstance(container, ast.Dict) and isinstance(idx, ast.Constant):
            for k2, v2 in zip(container.keys, container.values, strict=True):
                if isinstance(k2, ast.Constant) and k2.value == idx.value:
                    return _accessor_of_expr(v2, rooted, acc, depth + 1)
        return None
    return None


def _accessor_prim(fn: ast.AST, prov: _Provenance) -> str | None:
    """B321: primitivo de acceso dinámico ('getattr'/'vars'/'attrgetter'/'methodcaller'/'partial') al que resuelve `fn`.
    Delega en `_accessor_of_expr` (MISMA procedencia semántica) → cubre builtin bare, `operator.attrgetter`/
    `functools.partial`, `from operator import attrgetter`, y CUALQUIER alias (transitivo/from-import/contenedor/IfExp/
    walrus/destructuring). `_ProvenanceLimitError` propaga (fail-closed en `scan_reflection`)."""
    return _accessor_of_expr(fn, prov.rooted, prov.prim_aliases, 0)


def _call_reads_factory_module(call: ast.Call, prov: _Provenance) -> bool:
    """B317: la llamada obtiene un atributo de builtins/importlib de forma DINÁMICA — nombre literal o CALCULADO:
    `getattr(M,·)` · `vars(M)` · `attrgetter(·)(M)` · `methodcaller(·)(M)` · `partial(getattr|vars, M, ·)` — o construye
    un accesor por el NOMBRE LITERAL de una fábrica (`attrgetter('import_module')`, `methodcaller('__import__', …)`)."""
    fn = call.func
    # B321: getattr/vars — DIRECTO o por ALIAS (`g = getattr; g(M, ·)`) — aplicado a un módulo de fábrica
    if _accessor_prim(fn, prov) in ("getattr", "vars") and call.args and _factory_module_root(call.args[0], prov):
        return True
    if _accessor_prim(fn, prov) in ("attrgetter", "methodcaller") and call.args:  # attrgetter('import_module') LITERAL
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value in _CANONICAL_FACTORY_NAMES:
            return True
    if isinstance(fn, ast.Call) and _accessor_prim(fn.func, prov) in ("attrgetter", "methodcaller") and call.args and _factory_module_root(call.args[0], prov):  # fmt: skip
        return True  # el accesor construido (attrgetter/methodcaller) se APLICA a un módulo de fábrica
    if _accessor_prim(fn, prov) == "partial" and call.args:  # partial(getattr|vars, M, …) liga un accesor a M
        if _accessor_prim(call.args[0], prov) in ("getattr", "vars") and any(_factory_module_root(a, prov) for a in call.args[1:]):  # fmt: skip
            return True
    return False


def _subscript_reads_factory_module(sub: ast.Subscript, prov: _Provenance) -> bool:
    """B317/B324: `M.__dict__[·]` / `vars(M)[·]` / `__builtins__[·]` sobre builtins/importlib, y `sys.modules[·]` capaz de
    recuperar un módulo-fábrica (clave dinámica o `'builtins'`/`'importlib'` literal) — literal o calculado."""
    base = sub.value
    if isinstance(base, ast.Attribute) and base.attr == "__dict__" and _factory_module_root(base.value, prov):
        return True
    if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "vars" and base.args and _factory_module_root(base.args[0], prov):  # fmt: skip
        return True
    # B324: `sys.modules[k]` con clave NO literal (podría ser 'builtins'/'importlib') o literal de un módulo-fábrica →
    # recupera la fábrica → prohibido. `sys.modules['os']` (literal benigno) sigue su op registrable `sys.modules`.
    root = base.value if isinstance(base, ast.Attribute) and base.attr == "modules" else base
    is_sysmod = (isinstance(base, ast.Attribute) and base.attr == "modules" and isinstance(root, ast.Name) and prov.rooted.get(root.id) == "sys") or (isinstance(base, ast.Name) and base.id in prov.sysmod)  # fmt: skip
    if is_sysmod:
        idx = sub.slice
        if not (isinstance(idx, ast.Constant) and isinstance(idx.value, str)) or idx.value in _FACTORY_MODULE_ROOTS:
            return True
    return _factory_module_root(sub.value, prov) is not None  # `__builtins__['__import__']`, `importlib[...]`, etc.


def _dynamic_factory_escape(node: ast.AST, parents: dict[int, ast.AST], prov: _Provenance) -> str | None:
    """B308/B316/B317: prohibición GLOBAL, en el ORIGEN sintáctico, de toda fábrica estándar de import dinámico y de todo
    lookup dinámico sobre los módulos que las exponen (builtins/importlib). NO existe forma segura — `deep_smoke` ya no usa
    `importlib.import_module`, así que producción NO necesita ninguna fábrica dinámica. Prohibir en el origen sintáctico
    (ImportFrom/Name/Attribute/lookup) hace innecesario rastrear transformaciones posteriores: el programa ya es inválido
    donde OBTIENE la fábrica. Alcance honesto (§7.3): NO es un sandbox contra quien reimplemente un importador o ejecute
    código por otra API — esa autoridad es B291 + revisión. Devuelve ops PROHIBIDAS (no registrables)."""
    # (1) `__import__` / `__builtins__` como referencia ejecutable en Load — SIEMPRE prohibido (llamado o no)
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in ("__import__", "__builtins__"):
        return _DYNAMIC_IMPORT_FACTORY_VALUE
    # (1b) B324: ADQUIRIR el módulo-fábrica RAÍZ (`import builtins[.x]`/`import importlib[.x]` sin alias, o `import
    # builtins|importlib as X`) da acceso a `import_module`/`__import__` y está PROHIBIDO en producción. Los SUBMÓDULOS
    # con ALIAS explícito (`import importlib.metadata as X`) NO ligan la raíz y NO exponen la fábrica → permitidos.
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.name.split(".")[0] in _FACTORY_MODULE_ROOTS and (a.asname is None or a.name in _FACTORY_MODULE_ROOTS):
                return _DYNAMIC_IMPORT_FACTORY_VALUE
    # (2) `from builtins|importlib import __import__|import_module` (cualquier alias) — prohibido en el ImportFrom
    if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in _FACTORY_MODULE_ROOTS and any(a.name in _CANONICAL_FACTORY_NAMES for a in node.names):  # fmt: skip
        return _DYNAMIC_IMPORT_FACTORY_VALUE
    # (3) CUALQUIER Attribute `.import_module`/`.__import__` (§7.2) — prohibido SIEMPRE, sin importar la procedencia del
    # objeto (directo `importlib.import_module`, alias, rebind, o lavado por función identidad `ident(builtins).__import__`):
    # capturado, llamado o descartado. Los nombres de atributo de las fábricas estándar no tienen uso legítimo en producción.
    if isinstance(node, ast.Attribute) and node.attr in _CANONICAL_FACTORY_NAMES:
        return _DYNAMIC_IMPORT_FACTORY_VALUE
    # (4) lookup dinámico sobre builtins/importlib como LLAMADA (getattr/vars/attrgetter/methodcaller/partial) — literal o calculado
    if isinstance(node, ast.Call) and _call_reads_factory_module(node, prov):
        return _DYNAMIC_IMPORT_FACTORY_VALUE
    # (5) lookup dinámico sobre builtins/importlib como SUBSCRIPT (`M.__dict__[·]`, `vars(M)[·]`, `__builtins__[·]`)
    if isinstance(node, ast.Subscript) and _subscript_reads_factory_module(node, prov):
        return _DYNAMIC_IMPORT_FACTORY_VALUE
    # (6) DENY-BY-DEFAULT: toda llamada a una fábrica (bare/aliased `__import__`, `import_module` aliased) — sin excepción
    if isinstance(node, ast.Call) and _factory_root(node.func, prov.factories, prov.rooted) is not None:
        return _DYNAMIC_MODULE_RESULT_ESCAPE
    return None


def _canonical_provenance(tree: ast.AST, mod_aliases: dict[str, str]) -> _Provenance:
    """B294: análisis conservador de PROCEDENCIA canónica por FIXPOINT — NO otra lista de aliases. Cubre módulos,
    submódulos, imports `from` de miembros/clases/fábricas, `__import__`/`import_module` directos o aliased y sus
    RESULTADOS ligados a un binding, `sys.modules` y `<canónico>.__dict__` importados/aliased. Alimenta las políticas de
    call, subscript y escape. `from <canónico> import *` lo rechaza `scan_reflection`. Excluye primitivos específicos
    (getattr/attrgetter/partial mantienen su op)."""
    rooted: dict[str, str] = dict(mod_aliases)
    factories: dict[str, str] = {"__import__": "builtins"}  # `__import__` bare siempre es una fábrica (builtin)
    sysmod: set[str] = set()
    dictnames: set[str] = set()
    members: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _CANONICAL_MODULES:
                    rooted[a.asname or root] = root
        elif isinstance(node, ast.ImportFrom) and node.module:
            root0 = node.module.split(".")[0]
            if root0 in _CANONICAL_MODULES:
                for a in node.names:
                    if a.name == "*":
                        continue  # el rechazo lo hace scan_reflection
                    local = a.asname or a.name
                    rooted[local] = root0
                    members.add(local)  # B294: miembro traído por `from <canónico> import …`
                    if a.name in _CANONICAL_FACTORY_NAMES:
                        factories[local] = root0  # from importlib import import_module as im → im es fábrica
                    if root0 == "sys" and a.name == "modules":
                        sysmod.add(local)  # from sys import modules as mods
                    if a.name == "__dict__":
                        dictnames.add(local)  # from builtins import __dict__ as ns
    changed = True
    while changed:  # FIXPOINT
        changed = False
        for tgt, val in _name_bindings(tree):
            # fábrica por alias de Attribute (`factory = importlib.import_module`) o transitivo (`f2 = im`)
            if tgt not in factories:
                fr = _factory_root(val, factories, rooted) if isinstance(val, ast.Attribute) else None
                if fr is not None or (isinstance(val, ast.Name) and val.id in factories):
                    factories[tgt] = fr if fr is not None else factories[val.id]
                    changed = True
            if tgt not in sysmod and isinstance(val, ast.Attribute) and val.attr == "modules":
                if isinstance(val.value, ast.Name) and rooted.get(val.value.id) == "sys":
                    sysmod.add(tgt)
                    changed = True
            if tgt not in dictnames and isinstance(val, ast.Attribute) and val.attr == "__dict__":
                base: ast.AST = val.value
                while isinstance(base, (ast.Attribute, ast.Subscript)):
                    base = base.value
                if isinstance(base, ast.Name) and base.id in rooted:
                    dictnames.add(tgt)
                    changed = True
            if tgt not in rooted and tgt not in factories:  # B297: RHS COMPUESTA (Name/Call-fábrica/Attribute/IfExp/
                k, r = _expr_provenance(
                    val, rooted, factories, 0
                )  # BoolOp/subscript de contenedor) que evalúa a un valor
                if (
                    k == "value" and r is not None
                ):  # canónico → el binding queda rooteado (resultado de fábrica incluido)
                    rooted[tgt] = r
                    changed = True
    for name in _prim_aliases(tree):  # primitivo específico (getattr/attrgetter/partial…) — no dupliques como rooted
        rooted.pop(
            name, None
        )  # NOTA: NO se quita de `factories` — `__import__`/`import_module` son prim (op propia por
        members.discard(
            name
        )  # `_resolve_op` sobre la LLAMADA) Y fábrica (su RESULTADO queda rooteado, B297); nodos distintos
    # B321: procedencia de ACCESO — semilla de `_prim_aliases` (Name-transitivo + from-import), extendida por FIXPOINT
    # con `_accessor_of_expr` (Attribute de módulo, contenedor literal, IfExp/BoolOp all-same, walrus, destructuring).
    prim_aliases = {n: op for n, op in _prim_aliases(tree).items() if op in _ACCESSOR_PRIMS}
    changed = True
    while changed:
        changed = False
        for tgt, val in _name_bindings(tree):
            if tgt not in prim_aliases:
                p = _accessor_of_expr(val, rooted, prim_aliases, 0)
                if p is not None:
                    prim_aliases[tgt] = p
                    changed = True
    return _Provenance(rooted, frozenset(sysmod), frozenset(dictnames), frozenset(members), factories, prim_aliases)


def _canonical_member_escape(node: ast.AST, parents: dict[int, ast.AST], prov: _Provenance) -> str | None:
    """B294: un miembro canónico traído por `from <canónico> import <miembro>` que se CAPTURA como VALOR (asignado a otro
    nombre, pasado, retornado, en un contenedor) — no accedido (`.attr`), ni subscriptado, ni llamado — es una ocurrencia
    (`reflection-module-escape`). Ej. `from importlib import machinery; x = machinery`. Las formas call/attr/subscript
    las cubren las otras políticas; una llamada (`version('pkg')`) NO se marca aquí (es `func` de un Call)."""
    if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in prov.members):
        return None
    parent = parents.get(id(node))
    if parent is None:
        return None
    if isinstance(parent, ast.Attribute) and parent.value is node:
        return None  # `miembro.attr` — acceso resuelto
    if isinstance(parent, ast.Subscript) and parent.value is node:
        return None  # `miembro[...]` — subscript (cubierto por _canonical_rooted_subscript si aplica)
    if isinstance(parent, ast.Call) and parent.func is node:
        return None  # `miembro(...)` — llamada (cubierta por _canonical_rooted_call)
    return _REFLECTION_MODULE_ESCAPE


def _canonical_rooted_call(node: ast.AST, prov: _Provenance) -> str | None:
    """B285/B289: TODA `ast.Call` cuyo callee, al DESENVOLVER `.func`/`.value`, esté ROOTEADO en un nombre canónico
    ESTÁTICO (módulo/submódulo/miembro `from … import`) produce `canonical-rooted-call` (SALVO op más específica; orden
    del `or`). Ej.: `machinery.SourceFileLoader(...).set_data(...)`, `Loader(...).set_data(...)`. B308: un resultado de
    fábrica DINÁMICA en la cadena NO se rootea aquí — lo prohíbe `_dynamic_factory_escape`."""
    if not isinstance(node, ast.Call):
        return None
    cur: ast.AST = node.func
    while isinstance(cur, (ast.Attribute, ast.Subscript, ast.Call)):
        cur = cur.func if isinstance(cur, ast.Call) else cur.value
    if isinstance(cur, ast.Name) and isinstance(cur.ctx, ast.Load) and cur.id in prov.rooted:
        return _CANONICAL_ROOTED_CALL
    return None


def _canonical_rooted_subscript(node: ast.AST, prov: _Provenance) -> str | None:
    """B294: un `X[...]` cuya raíz (al desenvolver Attribute/Subscript) es un alias de `sys.modules` → `sys.modules`; de
    un `<canónico>.__dict__` → `__dict__`. Cubre `from sys import modules as mods; mods[k]` y `from builtins import
    __dict__ as ns; ns[k]`, invisibles cuando el import trae el miembro directamente."""
    if not isinstance(node, ast.Subscript):
        return None
    cur: ast.AST = node.value
    while isinstance(cur, (ast.Attribute, ast.Subscript, ast.Call)):
        cur = cur.func if isinstance(cur, ast.Call) else cur.value
    if isinstance(cur, ast.Name):
        if cur.id in prov.sysmod:
            return "sys.modules"
        if cur.id in prov.dictnames:
            return "__dict__"
    return None


def scan_reflection(files: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Escanea `files` y devuelve `(entries, problems)`. Cada ocurrencia lleva identidad SEMÁNTICA. Fail-closed: un
    fichero ilegible/no-UTF-8/no-parseable produce un PROBLEMA (no se salta en silencio)."""
    entries: dict[str, dict] = {}
    problems: list[str] = []
    for rel in files:
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            problems.append(f"{rel}: ilegible ({exc}) (fail-closed B260)")
            continue
        except UnicodeDecodeError as exc:
            problems.append(f"{rel}: no es UTF-8 ({exc}) (fail-closed B260)")
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            problems.append(f"{rel}: SyntaxError ({exc}) (fail-closed B260)")
            continue
        mod_aliases = _module_aliases(tree)
        try:
            prov = _canonical_provenance(tree, mod_aliases)  # B294: procedencia (calls/subscripts/escapes/fábricas)
        except _ProvenanceLimitError as exc:  # B300: la cota de análisis es un PROBLEMA fail-closed, no `none`
            problems.append(f"{rel}: análisis de procedencia excedió su cota ({exc}) (fail-closed B300)")
            continue
        prim_aliases = _prim_aliases(tree)
        qn = _qualnames(tree)
        stmts = _enclosing_stmts(tree)
        parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        # B294: `from <canónico> import *` es un ROMPE-procedencia — no se puede seguir qué nombres quedan rooteados.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in _CANONICAL_MODULES:
                if any(a.name == "*" for a in node.names):
                    problems.append(f"{rel}::{node.module}: `from {node.module} import *` prohibido — rompe la procedencia de reflexión (fail-closed B294)")  # fmt: skip
        raw: list[tuple[str, str, str, int, ast.AST]] = []
        try:
            for node in ast.walk(tree):
                op = (
                    _dynamic_factory_escape(
                        node, parents, prov
                    )  # B308: DENY-BY-DEFAULT de resultados de fábrica dinámica
                    or _resolve_op(node, mod_aliases, prim_aliases)
                    or _escape_op(node, parents, prov.rooted)  # B294: escape de módulo/miembro canónico (Name)
                    or _rooted_chain_escape(node, parents, mod_aliases)
                    or _canonical_rooted_call(node, prov)  # B285/B289: llamada rooteada en un canónico ESTÁTICO
                    or _canonical_rooted_subscript(node, prov)  # B294: sys.modules/__dict__ importados y subscriptados
                    or _canonical_member_escape(
                        node, parents, prov
                    )  # B294: captura no-call de un miembro `from … import`
                )
                if op is None:
                    continue
                qualname = qn.get(id(node), "") or "<module>"
                stmt = stmts.get(id(node))
                stmt_sha = _norm_stmt_sha(stmt) if stmt is not None else "0" * 64
                raw.append((qualname, op, stmt_sha, getattr(node, "lineno", -1), node))
        except _ProvenanceLimitError as exc:  # B300: fail-closed, nunca `none` silencioso
            problems.append(f"{rel}: análisis de procedencia excedió su cota ({exc}) (fail-closed B300)")
            continue
        # occurrence_index estable: ordena por (qualname, op, stmt_sha, lineno); el índice desempata idénticos
        raw.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
        seen: dict[tuple[str, str, str], int] = {}
        for qualname, op, stmt_sha, _lineno, node in raw:
            key = (qualname, op, stmt_sha)
            idx = seen.get(key, 0)
            seen[key] = idx + 1
            eid = _occurrence_id(rel, qualname, op, stmt_sha, idx)
            try:
                snippet = ast.unparse(node)
            except ValueError, AttributeError:
                snippet = op
            entries[eid] = {
                "file": rel,
                "qualname": qualname,
                "op": op,
                "statement_ast_sha256": stmt_sha,
                "occurrence_index": idx,
                "snippet": snippet[:120],
                "lineno": _lineno,
            }
    return entries, problems


def _resolve_relative(rel: str, level: int, module: str | None) -> str | None:
    """Resuelve `from … import` relativo a un módulo absoluto usando la ruta del fichero y `node.level`. None si el
    número de puntos escapa del árbol."""
    pkg = rel.split("/")[:-1]  # componentes de directorio = paquete del fichero
    if level == 0:
        return module
    if level - 1 > len(pkg):
        return None
    base = pkg[: len(pkg) - (level - 1)]
    return ".".join([*base, module]) if module else ".".join(base)


def scan_cb_importers(files: list[str]) -> tuple[set[str], list[str]]:
    """Ficheros de producción que IMPORTAN `tools.campaign_bundle` en cualquier forma (absoluta o RELATIVA). Fail-closed
    ante ilegible/no-parseable."""
    importers: set[str] = set()
    problems: list[str] = []
    for rel in files:
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            problems.append(f"{rel}: no escaneable para importadores ({exc}) (fail-closed B260)")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == _CB_MODULE for a in node.names):
                importers.add(rel)
            elif isinstance(node, ast.ImportFrom):
                mod = _resolve_relative(rel, node.level, node.module)
                if mod == _CB_MODULE:
                    importers.add(rel)  # `from tools.campaign_bundle import …` (abs o relativo)
                elif mod == "tools" and any(a.name == "campaign_bundle" for a in node.names):
                    importers.add(rel)  # `from tools import campaign_bundle`
    return importers, problems


def _load_registry() -> tuple[dict, list[str]]:
    try:
        with open(os.path.join(ROOT, _REGISTRY), encoding="utf-8") as fh:
            return json.loads(fh.read(), object_pairs_hook=_no_dup_pairs), []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{_REGISTRY}: ilegible/no-JSON/duplicado ({exc}) (fail-closed B255/B260)"]


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def _today() -> datetime.date:
    return datetime.date.today()


def problems() -> list[str]:
    """Gate fail-closed: esquema del registro exacto; cada ocurrencia de reflexión de producción declarada por su ID
    SEMÁNTICO con metadatos coincidentes y `review_by` no expirado; ningún importador de `tools.campaign_bundle` fuera
    de la lista positiva. Cualquier ocurrencia nueva/cambiada, entrada obsoleta, metadato divergente, error de
    lectura/parseo, o importador no autorizado → problema estructurado."""
    reg, errs = _load_registry()
    if errs:
        return errs
    if not isinstance(reg, dict) or set(reg) != _REGISTRY_TOP_KEYS:
        return [f"{_REGISTRY}: claves superiores != {sorted(_REGISTRY_TOP_KEYS)} (fail-closed B255)"]
    if not (type(reg["schema_version"]) is int and reg["schema_version"] == _SCHEMA_VERSION):
        return [f"{_REGISTRY}: schema_version no es {_SCHEMA_VERSION}"]
    if not (type(reg["scanner_version"]) is int and reg["scanner_version"] == _SCANNER_VERSION):
        return [f"{_REGISTRY}: scanner_version no es {_SCANNER_VERSION}"]
    if list(reg["operations_controlled"]) != list(OPERATIONS_CONTROLLED):
        return [f"{_REGISTRY}: operations_controlled != el set del scanner"]
    entries = reg["entries"]
    authorized = reg["authorized_campaign_bundle_importers"]
    if not isinstance(entries, dict) or not isinstance(authorized, list):
        return [f"{_REGISTRY}: 'entries'/'authorized_campaign_bundle_importers' con tipo inválido"]

    files = _production_files()
    if not files:
        return ["git ls-files no devolvió .py de producción (fail-closed B255)"]
    problems: list[str] = []

    observed, scan_probs = scan_reflection(files)
    problems.extend(scan_probs)
    today = _today()
    for eid, occ in observed.items():
        # B308/B310: fábrica dinámica PROHIBIDA globalmente (no registrable) — ni su resultado fuera del descarte local
        # seguro, ni la fábrica capturada como VALOR, se pueden blanquear con una entrada de registro.
        if occ["op"] in (_DYNAMIC_MODULE_RESULT_ESCAPE, _DYNAMIC_IMPORT_FACTORY_VALUE):
            _b = "B310" if occ["op"] == _DYNAMIC_IMPORT_FACTORY_VALUE else "B308"
            problems.append(f"FÁBRICA DINÁMICA PROHIBIDA ({occ['op']}): {occ['file']}::{occ['qualname']} línea {occ['lineno']} → `{occ['snippet']}` ({_b})")  # fmt: skip
            continue
        # B265: en los módulos de AUTORIDAD, un escape de módulo / lookup dinámico está PROHIBIDO (no registrable)
        if occ["file"] in _AUTHORITY_MODULES and occ["op"] in (_REFLECTION_MODULE_ESCAPE, _BUILTINS_DYNAMIC_LOOKUP):
            problems.append(f"ESCAPE/LOOKUP DINÁMICO PROHIBIDO en módulo de autoridad: {occ['op']} en {occ['file']}::{occ['qualname']} línea {occ['lineno']} (B265)")  # fmt: skip
            continue
        want = entries.get(eid)
        if want is None:
            problems.append(f"REFLEXIÓN NO REGISTRADA: {occ['op']} en {occ['file']}::{occ['qualname']} línea {occ['lineno']} → `{occ['snippet']}` (registrar en {_REGISTRY}) (B255/B259)")  # fmt: skip
            continue
        if not (isinstance(want, dict) and set(want) == _ENTRY_KEYS):
            problems.append(f"{_REGISTRY}[{eid[:12]}…]: claves de entrada != {sorted(_ENTRY_KEYS)}")
            continue
        for field in ("file", "qualname", "op", "statement_ast_sha256", "occurrence_index"):
            if want.get(field) != occ[field]:
                problems.append(f"{_REGISTRY}[{eid[:12]}…]: {field} registrado ({want.get(field)!r}) != derivado ({occ[field]!r})")  # fmt: skip
        if not (isinstance(want.get("justification"), str) and want["justification"].strip()):
            problems.append(f"{_REGISTRY}[{eid[:12]}…]: justification vacía")
        rb = want.get("review_by")
        try:
            rb_date = datetime.date.fromisoformat(rb) if isinstance(rb, str) else None
        except ValueError:
            rb_date = None
        if rb_date is None:
            problems.append(f"{_REGISTRY}[{eid[:12]}…]: review_by no es una fecha ISO")
        elif rb_date < today:
            problems.append(f"{_REGISTRY}[{eid[:12]}…]: review_by {rb} EXPIRADO — re-revisar la reflexión (B255)")
    for eid in entries:
        if eid not in observed:
            w = entries.get(eid, {})
            problems.append(f"entrada de reflexión OBSOLETA ({w.get('op')} en {w.get('file')}::{w.get('qualname')}) — ya no existe (B255)")  # fmt: skip

    observed_imp, imp_probs = scan_cb_importers(files)
    problems.extend(imp_probs)
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
    print(
        f"✓ toda reflexión de producción está registrada (identidad semántica); importadores autorizados ({_REGISTRY})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
