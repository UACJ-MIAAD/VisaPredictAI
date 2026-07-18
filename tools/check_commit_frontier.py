#!/usr/bin/env python
"""Gate ESTRUCTURAL de la FRONTERA DE COMMIT (P0R.5 · Incremento 2), fail-closed sobre `tools/merge_campaign_pools.py`.

La autoridad del commit es el `CommitCertificate` de CURRENT, NO el recibo. Este gate verifica ESTÁTICAMENTE que la
maquinaria del merge respete esa frontera:

1. `commit_reached` NUNCA se ASIGNA (es una `@property` DERIVADA del latch `_committed`).
2. El latch `_committed = True` aparece EXACTAMENTE dos veces, y SÓLO dentro de `mark_current_certified` y
   `mark_committed_incomplete` (métodos de `_TxContext`).
3. B222/B223: `mark_current_certified` y `mark_committed_incomplete` VALIDAN el cert con `_validate_commit_certificate`
   (isinstance CommitCertificate + `durability_state` durable + hashes) — jamás un bool `authority_crossed` (duck typing).
4. `_certify_receipt` (el recibo = EVIDENCIA) JAMÁS toca el estado comprometido (`_committed` /
   `mark_current_certified` / `mark_committed_incomplete`).
5. `mark_current_certified(...)` se llama EXACTAMENTE una vez en todo el módulo (punto de commit único).
6. B221: TODA llamada a `_rollback()` está GUARDADA por `rollback_allowed` (jamás corre tras el certificado NI en
   estado INDETERMINADO).
7. B222/B223: PROHIBIDO `getattr(x, "authority_crossed")`; la clasificación post-publish decide por TIPO estructurado
   (`except _bundle.CommittedStateError`/`AuthorityIndeterminateError`), nunca por texto de excepción.
8. B221: existen el terminal `AUTHORITY_INDETERMINATE`, `mark_indeterminate` y la property `rollback_allowed`.

Escanea SÓLO el fichero versionado; si git falla o no parsea, FALLA cerrado.
"""

from __future__ import annotations

import ast
import subprocess
import sys

_TARGET = "tools/merge_campaign_pools.py"
_LATCH_METHODS = ("mark_current_certified", "mark_committed_incomplete")


def _git_tracked(rel: str) -> bool:
    try:
        out = subprocess.run(["git", "ls-files", "--error-unmatch", rel], capture_output=True, text=True)
    except OSError:
        return False
    return out.returncode == 0


def _within(fn: ast.FunctionDef | None, node: ast.AST) -> bool:
    return fn is not None and fn.lineno <= getattr(node, "lineno", -1) <= (fn.end_lineno or fn.lineno)


def _names_in(node: ast.AST, names: tuple[str, ...]) -> bool:
    """True si algún Name/Attribute/constante-string del subárbol coincide con uno de `names` (cubre
    `getattr(x, "authority_crossed", …)` donde el nombre viaja como string literal)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in names:
            return True
        if isinstance(n, ast.Attribute) and n.attr in names:
            return True
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in names:
            return True
    return False


def frontier_problems(src: str) -> list[str]:
    tree = ast.parse(src)
    funcs = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)}
    problems: list[str] = []

    # 1. commit_reached NUNCA se asigna (property derivada)
    cr_assigns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "commit_reached" for t in n.targets)
    ]
    if cr_assigns:
        problems.append(f"commit_reached se asigna en {len(cr_assigns)} sitio(s) — debe ser property derivada")
    if "def commit_reached" not in src or "@property" not in src:
        problems.append("commit_reached debe ser una @property derivada del latch")

    # 2. el latch _committed = True: EXACTAMENTE 2 veces, sólo en los métodos del latch
    latch = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_committed" for t in n.targets)
        and isinstance(n.value, ast.Constant)
        and n.value.value is True
    ]
    if len(latch) != 2:
        problems.append(f"el latch _committed=True aparece {len(latch)} veces (debe ser 2)")
    for a in latch:
        if not any(_within(funcs.get(m), a) for m in _LATCH_METHODS):
            problems.append(f"_committed=True (línea {a.lineno}) fuera de {_LATCH_METHODS}")

    # 3. mark_current_certified y mark_committed_incomplete VALIDAN (semántica) Y CONSUMEN (procedencia de fábrica +
    # uso único, B234) el certificado — no duck typing por authority_crossed, y la forma correcta no basta.
    for m in ("mark_current_certified", "mark_committed_incomplete"):
        fn = funcs.get(m)
        if fn is None or not _names_in(fn, ("_validate_commit_certificate",)):
            problems.append(f"{m} debe validar el cert con _validate_commit_certificate (no un bool authority_crossed)")
        if fn is None or not _names_in(fn, ("_consume_issued_certificate",)):
            problems.append(f"{m} debe CONSUMIR el cert con _consume_issued_certificate (B234: procedencia de fábrica + uso único)")  # fmt: skip
    vcc = funcs.get("_validate_commit_certificate")
    if vcc is None or not _names_in(vcc, ("CommitCertificate", "durability_state")):
        problems.append(
            "_validate_commit_certificate debe exigir isinstance(CommitCertificate) + durability_state durable"
        )
    # B226: la validación del cert NO puede reducirse a tipo+durabilidad — debe cubrir los campos SEMÁNTICOS (linaje,
    # campaña, ambos inodes), o un cert real con basura en esos campos pasaría. Estos nombres viven SÓLO en el bloque
    # semántico (no en el bucle de hashes), así que su ausencia delata que se eliminó la validación B226.
    elif not all(_names_in(vcc, (f,)) for f in ("previous_bundle_id", "campaign_id", "pointer_inode", "bundle_inode")):  # fmt: skip
        problems.append("_validate_commit_certificate debe validar los campos semánticos (B226): previous_bundle_id, campaign_id y ambos inodes")  # fmt: skip

    # 4. _certify_receipt no toca el estado comprometido
    cr = funcs.get("_certify_receipt")
    if cr is None:
        problems.append("falta _certify_receipt (el recibo como evidencia)")
    else:
        forbidden = ("_committed", "mark_current_certified", "mark_committed_incomplete")
        if _names_in(cr, forbidden):
            problems.append("_certify_receipt toca el estado comprometido (debe ser sólo evidencia)")

    # 5. mark_current_certified se LLAMA exactamente una vez (punto de commit único)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "mark_current_certified"
    ]
    if len(calls) != 1:
        problems.append(f"mark_current_certified se llama {len(calls)} veces (debe ser 1 — commit único)")

    # 6. toda llamada a _rollback() está guardada por una condición que menciona `rollback_allowed` (B221: jamás
    # corre en estado cruzado NI indeterminado)
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _names_in(node.test, ("rollback_allowed",)):
            for stmt in node.body:
                for c in ast.walk(stmt):
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_rollback":
                        guarded.add(id(c))
    all_rollback_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_rollback"  # fmt: skip
    ]
    for c in all_rollback_calls:
        if id(c) not in guarded:
            problems.append(f"_rollback() en línea {c.lineno} NO está guardado por `rollback_allowed`")

    # 7. NADIE decide el cruce por `authority_crossed` (duck typing) ni por texto: prohibido getattr(x,'authority_crossed')
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:  # fmt: skip
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and arg.value == "authority_crossed":
                problems.append(f"getattr(..., 'authority_crossed') en línea {node.lineno} (clasificación por duck typing)")  # fmt: skip
    # el clasificador post-publish decide por TIPOS estructurados (CommittedStateError/AuthorityIndeterminateError)
    if "except _bundle.CommittedStateError" not in src or "except _bundle.AuthorityIndeterminateError" not in src:
        problems.append("la clasificación post-publish debe usar `except _bundle.CommittedStateError`/`AuthorityIndeterminateError` (por tipo)")  # fmt: skip
    for handler in ast.walk(tree):  # ningún handler de la clasificación del cruce ramifica por texto de excepción
        if isinstance(handler, ast.ExceptHandler) and _names_in(handler, ("mark_committed_incomplete", "mark_indeterminate")) and _decides_by_text(handler):  # fmt: skip
            problems.append("la clasificación del cruce decide por TEXTO de excepción (str/args)")

    # 8. terminal AUTHORITY_INDETERMINATE + mark_indeterminate + rollback_allowed existen (B221)
    if "_S_AUTHORITY_INDETERMINATE" not in src or funcs.get("mark_indeterminate") is None:
        problems.append("debe existir el terminal AUTHORITY_INDETERMINATE + mark_indeterminate")
    if funcs.get("rollback_allowed") is None:
        problems.append("debe existir la property rollback_allowed (rollback SÓLO si no cruzó y no indeterminado)")

    # 9. B231: el merge JAMÁS construye un CommitCertificate — sólo consume el que EMITE la fábrica del módulo bundle.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "CommitCertificate") or (isinstance(f, ast.Attribute) and f.attr == "CommitCertificate"):  # fmt: skip
                problems.append(f"construcción directa de CommitCertificate en {_TARGET}:{node.lineno} (sólo la fábrica _build_certificate la emite)")  # fmt: skip
    return problems


_FACTORY_TARGET = "tools/campaign_bundle.py"
_FACTORY_FN = "_build_certificate"


def factory_problems(src: str) -> list[str]:
    """B231/B234: en `campaign_bundle`, `CommitCertificate(...)` se construye SÓLO dentro de la fábrica
    `_build_certificate`; la fábrica REGISTRA el cert (`_register_certificate`) y nadie más lo llama; el registro de
    procedencia `_ISSUED_CERTS` sólo se muta dentro de `_register_certificate` (write) y `consume_commit_certificate`
    (del) — ningún otro sitio lo toca (un cert es autoridad sólo si lo emitió la fábrica y no se ha consumido)."""
    tree = ast.parse(src)
    funcs = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)}
    factory = funcs.get(_FACTORY_FN)
    problems: list[str] = []
    if factory is None:
        return [f"falta la fábrica {_FACTORY_FN} en {_FACTORY_TARGET}"]

    def _in(fn: ast.FunctionDef | None, node: ast.AST) -> bool:
        ln = getattr(node, "lineno", -1)
        return fn is not None and fn.lineno <= ln <= (fn.end_lineno or fn.lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CommitCertificate":
            if not _in(factory, node):
                problems.append(f"CommitCertificate construido FUERA de {_FACTORY_FN} en {_FACTORY_TARGET}:{node.lineno}")  # fmt: skip
        # B234: _register_certificate se LLAMA sólo desde la fábrica (registro de procedencia controlado)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_register_certificate":
            if not _in(factory, node):
                problems.append(f"_register_certificate llamado FUERA de {_FACTORY_FN} en {_FACTORY_TARGET}:{node.lineno}")  # fmt: skip
        # B234: el registro _ISSUED_CERTS sólo se MUTA (subscript store / del) en _register_certificate y consume_commit_certificate
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "_ISSUED_CERTS" and isinstance(node.ctx, (ast.Store, ast.Del)):  # fmt: skip
            if not (
                _in(funcs.get("_register_certificate"), node) or _in(funcs.get("consume_commit_certificate"), node)
            ):
                problems.append(f"_ISSUED_CERTS mutado FUERA de _register_certificate/consume_commit_certificate en {_FACTORY_TARGET}:{node.lineno}")  # fmt: skip
    return problems


_AUTHORITY_PRIMITIVES = ("_register_certificate", "_ISSUED_CERTS", "consume_commit_certificate")
_GATE_SELF = "tools/check_commit_frontier.py"
# B242: allowlist POR OCURRENCIA. `{módulo: {primitiva: {funciones permitidas}}}`; `None` = nivel de módulo
# (definición/anotación). Un uso de una primitiva FUERA de estos sitios exactos —en CUALQUIER módulo, incluidos la
# fábrica y el consumidor— es una violación. Ya no hay módulos exentos por bloque; sólo el propio gate y `tests/`.
_AUTHORITY_ALLOW: dict[str, dict[str, frozenset]] = {
    _FACTORY_TARGET: {  # campaign_bundle: la fábrica registra; register/consume mutan el registro; el resto NADA
        "_register_certificate": frozenset({"_build_certificate", "_register_certificate"}),
        "_ISSUED_CERTS": frozenset({"_register_certificate", "consume_commit_certificate", None}),
        "consume_commit_certificate": frozenset({"consume_commit_certificate"}),
    },
    _TARGET: {  # merge: SÓLO el wrapper `_consume_issued_certificate` consume
        "consume_commit_certificate": frozenset({"_consume_issued_certificate"}),
    },
}


def _git_tracked_py() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--", "*.py"], capture_output=True, text=True)
    except OSError:
        return []
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _const_str(node: ast.AST) -> str | None:
    """B242/B245: resuelve un nodo a un string CONSTANTE si es determinable estáticamente — literal, `a + b`, f-string
    constante, `"sep".join([const, …])` y `"pat".format(const, …)`. Cierra los bypass
    `getattr(x, "_reg" + "ister…")`, `"".join(["_reg", "ister…"])`, `"_reg{}".format("ister…")`."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        lft, rgt = _const_str(node.left), _const_str(node.right)
        return None if lft is None or rgt is None else lft + rgt
    if isinstance(node, ast.JoinedStr):  # f-string: constante SÓLO si TODAS sus partes lo son (incl. `{"const"}`)
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                inner = _const_str(v.value)
                if inner is None or v.format_spec is not None or v.conversion not in (-1, None):
                    return None
                parts.append(inner)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        recv = _const_str(node.func.value)  # el receptor debe ser un string constante
        if recv is not None and node.func.attr == "join" and len(node.args) == 1 and not node.keywords and isinstance(node.args[0], (ast.List, ast.Tuple)):  # fmt: skip
            jparts = [_const_str(e) for e in node.args[0].elts]
            if all(isinstance(p, str) for p in jparts):
                return recv.join(p for p in jparts if isinstance(p, str))
        if recv is not None and node.func.attr == "format" and not node.keywords:
            args = [_const_str(a) for a in node.args]
            if all(isinstance(a, str) for a in args):
                try:
                    return recv.format(*[a for a in args if isinstance(a, str)])
                except IndexError, KeyError, ValueError:
                    return None
    return None


def _campaign_bundle_aliases(tree: ast.AST) -> set[str]:
    """B245: nombres locales que refieren al MÓDULO `campaign_bundle` (`import tools.campaign_bundle as X`,
    `from tools import campaign_bundle as X`), para cazar acceso DINÁMICO a su superficie de autoridad."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "tools.campaign_bundle" and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module in ("tools", None):
            for a in node.names:
                if a.name == "campaign_bundle":
                    aliases.add(a.asname or "campaign_bundle")
    return aliases


def _enclosing_fn_name(funcs: list[ast.FunctionDef], lineno: int) -> str | None:
    best: ast.FunctionDef | None = None
    for fn in funcs:
        if fn.lineno <= lineno <= (fn.end_lineno or fn.lineno) and (best is None or fn.lineno > best.lineno):
            best = fn
    return best.name if best else None


def authority_scope_problems() -> list[str]:
    """B237/B242: ninguna referencia a las primitivas de autoridad del certificado (`_register_certificate`,
    `_ISSUED_CERTS`, `consume_commit_certificate`) ocurre fuera de su sitio EXACTO autorizado (allowlist POR OCURRENCIA,
    `_AUTHORITY_ALLOW`) — en NINGÚN módulo, incluidos la fábrica y el consumidor (ya NO hay exención por bloque). Cubre
    Name / Attribute / `from…import` / string CONSTANTE (literal, concatenación, f-string constante → cierra
    `getattr("_reg"+"ister…")` y `__dict__["_ISSUED_"+"CERTS"]`). `CommitCertificate(...)` se construye SÓLO en
    `_build_certificate`. `exec`/`eval`/`compile` PROHIBIDOS en los módulos de autoridad (resolución dinámica no
    verificable). Escanea TODOS los `.py` versionados (excluye sólo el gate y `tests/`). Fail-closed."""
    files = _git_tracked_py()
    if not files:
        return ["git ls-files no devolvió .py (fail-closed)"]
    problems: list[str] = []
    for rel in files:
        if rel == _GATE_SELF or rel.startswith("tests/"):
            continue
        try:
            with open(rel, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError) as exc:
            problems.append(f"✗ {rel}: ilegible/no parseable ({exc}) (fail-closed)")
            continue
        funcs = [fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)]
        allow = _AUTHORITY_ALLOW.get(rel, {})  # sin entrada → NINGÚN uso permitido en este módulo
        cb_aliases = _campaign_bundle_aliases(tree)  # B245: nombres que refieren al módulo campaign_bundle
        for node in ast.walk(tree):
            if rel in _AUTHORITY_ALLOW and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval", "compile"):  # fmt: skip
                problems.append(f"{rel}:{node.lineno} usa {node.func.id}() en un módulo de autoridad (resolución dinámica prohibida)")  # fmt: skip
            # B245: acceso DINÁMICO a la superficie de autoridad → fail-closed
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in cb_aliases:  # fmt: skip
                if len(node.args) < 2 or _const_str(node.args[1]) is None:  # nombre NO resoluble estáticamente
                    problems.append(f"{rel}:{node.lineno} getattr dinámico sobre el módulo campaign_bundle (fail-closed B245)")  # fmt: skip
            if isinstance(node, ast.Attribute) and node.attr == "__dict__" and isinstance(node.value, ast.Name) and node.value.id in cb_aliases:  # fmt: skip
                problems.append(f"{rel}:{node.lineno} accede a campaign_bundle.__dict__ (elude el gate B245)")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "import_module" and node.args and (_m := _const_str(node.args[0])) and "campaign_bundle" in _m:  # fmt: skip
                problems.append(f"{rel}:{node.lineno} importlib.import_module de campaign_bundle (elude el gate B245)")
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "modules" and (_k := _const_str(node.slice)) and "campaign_bundle" in _k:  # fmt: skip
                problems.append(f"{rel}:{node.lineno} sys.modules[...campaign_bundle...] (elude el gate B245)")
            # resuelve el/los nombre(s) de primitiva que este nodo referencia
            hit: str | None = None
            if isinstance(node, ast.Name) and node.id in _AUTHORITY_PRIMITIVES:
                hit = node.id
            elif isinstance(node, ast.Attribute) and node.attr in _AUTHORITY_PRIMITIVES:
                hit = node.attr
            elif isinstance(node, ast.ImportFrom) and any(a.name in _AUTHORITY_PRIMITIVES for a in node.names):
                problems.append(f"{rel}:{node.lineno} importa una primitiva de autoridad del certificado")
            else:
                cs = _const_str(node)
                hit = cs if cs in _AUTHORITY_PRIMITIVES else None
            if hit is not None:
                fn = _enclosing_fn_name(funcs, getattr(node, "lineno", -1))
                if fn == hit:  # la propia definición de la función homónima (cuerpo referenciándose)
                    pass
                elif fn in allow.get(hit, frozenset()):  # uso PERMITIDO por la allowlist por-ocurrencia
                    pass
                else:
                    problems.append(f"{rel}:{getattr(node, 'lineno', '?')} usa la primitiva de autoridad {hit!r} fuera de sitio autorizado (fn={fn})")  # fmt: skip
            if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == "CommitCertificate") or (isinstance(node.func, ast.Attribute) and node.func.attr == "CommitCertificate")):  # fmt: skip
                fn = _enclosing_fn_name(funcs, node.lineno)
                if not (rel == _FACTORY_TARGET and fn == "_build_certificate"):
                    problems.append(f"{rel}:{node.lineno} CONSTRUYE CommitCertificate fuera de _build_certificate")
    return problems
    return problems


def _within_node(outer: ast.AST, inner: ast.AST) -> bool:
    lo = getattr(outer, "lineno", -1)
    hi = getattr(outer, "end_lineno", lo)
    return lo <= getattr(inner, "lineno", -1) <= hi


def _decides_by_text(handler: ast.ExceptHandler) -> bool:
    """True si el manejador ramifica leyendo el TEXTO de la excepción (str(exc) / exc.args) — prohibido para decidir
    el cruce del commit."""
    name = handler.name
    for n in ast.walk(handler):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "str" and n.args:
            if isinstance(n.args[0], ast.Name) and n.args[0].id == name:
                return True
        if isinstance(n, ast.Attribute) and n.attr == "args" and isinstance(n.value, ast.Name) and n.value.id == name:
            return True
    return False


def main() -> int:
    if not _git_tracked(_TARGET):
        print(f"✗ {_TARGET}: NO versionado o git ls-files falló (fail-closed)")
        return 1
    try:
        with open(_TARGET, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        print(f"✗ {_TARGET}: ilegible ({exc}) (fail-closed)")
        return 1
    try:
        problems = frontier_problems(src)
    except SyntaxError as exc:
        print(f"✗ {_TARGET}: no parseable ({exc}) (fail-closed)")
        return 1
    if not _git_tracked(_FACTORY_TARGET):  # B231: la fábrica del certificado también fail-closed
        print(f"✗ {_FACTORY_TARGET}: NO versionado o git ls-files falló (fail-closed)")
        return 1
    try:
        with open(_FACTORY_TARGET, encoding="utf-8") as fh:
            problems += factory_problems(fh.read())
    except (OSError, SyntaxError) as exc:
        print(f"✗ {_FACTORY_TARGET}: ilegible/no parseable ({exc}) (fail-closed)")
        return 1
    problems += authority_scope_problems()  # B237: barrido del árbol versionado COMPLETO (nadie más toca la autoridad)
    if problems:
        print("✗ frontera de commit violada:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ frontera de commit íntegra (autoridad = CommitCertificate de CURRENT): {_TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
