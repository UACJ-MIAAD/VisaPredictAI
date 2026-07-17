#!/usr/bin/env python
"""Gate ESTRUCTURAL de la FRONTERA DE COMMIT (P0R.5 · Incremento 2), fail-closed sobre `tools/merge_campaign_pools.py`.

La autoridad del commit es el `CommitCertificate` de CURRENT, NO el recibo. Este gate verifica ESTÁTICAMENTE que la
maquinaria del merge respete esa frontera:

1. `commit_reached` NUNCA se ASIGNA (es una `@property` DERIVADA del latch `_committed`).
2. El latch `_committed = True` aparece EXACTAMENTE dos veces, y SÓLO dentro de `mark_current_certified` y
   `mark_committed_incomplete` (métodos de `_TxContext`).
3. `mark_current_certified` CONSUME un certificado (referencia `authority_crossed`) — el commit se declara con un
   `CommitCertificate` estructurado, no con texto.
4. `_certify_receipt` (el recibo = EVIDENCIA) JAMÁS toca el estado comprometido (`_committed` /
   `mark_current_certified` / `mark_committed_incomplete`).
5. `mark_current_certified(...)` se llama EXACTAMENTE una vez en todo el módulo (punto de commit único).
6. TODA llamada a `_rollback()` está GUARDADA por una condición que menciona `commit_reached` (jamás corre tras el
   certificado).
7. La rama que declara COMMITTED_INCOMPLETE decide por `authority_crossed` (evidencia ESTRUCTURADA), no por el texto
   de la excepción (`str(...)` / `.args` de una excepción).

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

    # 3. mark_current_certified consume un certificado (authority_crossed)
    mcc = funcs.get("mark_current_certified")
    if mcc is None or not _names_in(mcc, ("authority_crossed",)):
        problems.append("mark_current_certified debe consumir un CommitCertificate (authority_crossed)")

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

    # 6. toda llamada a _rollback() está guardada por una condición que menciona commit_reached
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _names_in(node.test, ("commit_reached",)):
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
            problems.append(f"_rollback() en línea {c.lineno} NO está guardado por `commit_reached`")

    # 7. la decisión de COMMITTED_INCOMPLETE usa authority_crossed y NO texto de excepción
    promote = funcs.get("_promote_transactionally")
    if promote is not None:
        mci_calls = [
            n
            for n in ast.walk(promote)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "mark_committed_incomplete"  # fmt: skip
        ]
        for handler in ast.walk(promote):
            if isinstance(handler, ast.ExceptHandler) and any(_within_node(handler, m) for m in mci_calls):
                if not _names_in(handler, ("authority_crossed",)):
                    problems.append("la rama COMMITTED_INCOMPLETE no decide por authority_crossed (evidencia estructurada)")  # fmt: skip
                if _decides_by_text(handler):
                    problems.append("la rama COMMITTED_INCOMPLETE decide por TEXTO de excepción (str/args)")
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
    if problems:
        print("✗ frontera de commit violada:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ frontera de commit íntegra (autoridad = CommitCertificate de CURRENT): {_TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
