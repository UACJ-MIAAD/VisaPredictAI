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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REGISTRY = "security/python_reflection_registry.json"
_SCHEMA_VERSION = 2
_SCANNER_VERSION = (
    5  # B265/B270/B276: escape de módulo + lookup dinámico builtins + cadenas enraizadas (escape O efecto descartado)
)
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
OPERATIONS_CONTROLLED = tuple(sorted(_BUILTIN_PRIMS | frozenset(_MODULE_PRIMS) | _ATTR_PRIMS | {"__dict__", "sys.modules", _REFLECTION_MODULE_ESCAPE, _BUILTINS_DYNAMIC_LOOKUP}))  # fmt: skip
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


def _name_bindings(tree: ast.AST):
    """Itera `(nombre, valor)` sobre Assign/AnnAssign/NamedExpr de un solo Name (base de los fixpoints de alias)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            yield node.targets[0].id, node.value
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
        prim_aliases = _prim_aliases(tree)
        qn = _qualnames(tree)
        stmts = _enclosing_stmts(tree)
        parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        raw: list[tuple[str, str, str, int, ast.AST]] = []
        for node in ast.walk(tree):
            op = (
                _resolve_op(node, mod_aliases, prim_aliases)
                or _escape_op(node, parents, mod_aliases)
                or _rooted_chain_escape(node, parents, mod_aliases)
            )
            if op is None:
                continue
            qualname = qn.get(id(node), "") or "<module>"
            stmt = stmts.get(id(node))
            stmt_sha = _norm_stmt_sha(stmt) if stmt is not None else "0" * 64
            raw.append((qualname, op, stmt_sha, getattr(node, "lineno", -1), node))
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
