#!/usr/bin/env python
"""B331: gate AST POSITIVO de la AUTORIDAD del smoke deep (`tools/deep_smoke.py`).

En el SHA base `evaluate()` construía el recibo a partir de un `DeepSmokeContract` suministrado por el CALLER, así que un
`for_test((('torch','torch'),))` reducía la autoridad a torch y emitía recibo verde. Este gate FAIL-CLOSED fija por
estructura que:

1. Existe EXACTAMENTE UNA construcción del literal del recibo (identificada por sus claves marcadoras) y vive DENTRO de la
   función emisora `certify_observation` — ninguna otra función lo construye.
2. `certify_observation` NO acepta parámetros de contrato del caller (`contract` / `contract_sha` / `contract_imports`).
3. `certify_observation` carga el contrato CANÓNICO por su cuenta (`load_contract()` en su cuerpo).
4. La función PURA `evaluate` NO construye recibo (sólo devuelve problemas).

Escanea SÓLO el fichero versionado `tools/deep_smoke.py`; si git falla o el fichero no parsea, FALLA cerrado. El núcleo
`problems(src)` es PURO/testeable con fuente inyectada (para regresiones adversariales)."""

from __future__ import annotations

import ast
import subprocess
import sys

_TARGET = "tools/deep_smoke.py"
_EMITTER = "certify_observation"
_PURE = "evaluate"
# Claves MARCADORAS que identifican inequívocamente el literal del recibo (no una config cualquiera).
_RECEIPT_MARKERS = frozenset({"deep_smoke_contract_sha256", "tensor_checksum", "lock_sha256"})
_FORBIDDEN_EMITTER_PARAMS = frozenset({"contract", "contract_sha", "contract_imports"})


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _is_receipt(node: ast.AST) -> bool:
    return isinstance(node, ast.Dict) and _RECEIPT_MARKERS <= _dict_keys(node)


def _func_owner(tree: ast.Module) -> dict[int, str]:
    """Mapea cada nodo al nombre de la FunctionDef de PRIMER nivel que lo contiene (el más externo gana)."""
    owner: dict[int, str] = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for sub in ast.walk(fn):
                owner.setdefault(id(sub), fn.name)
    return owner


def _params(fn: ast.FunctionDef) -> set[str]:
    return {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}


def problems(src: str) -> list[str]:
    try:
        tree = ast.parse(src, filename=_TARGET)
    except SyntaxError as exc:
        return [f"{_TARGET}: no parseable ({exc}) (fail-closed)"]
    probs: list[str] = []
    owner = _func_owner(tree)
    receipts = [n for n in ast.walk(tree) if _is_receipt(n)]
    if len(receipts) != 1:  # una y sólo una construcción del recibo
        probs.append(f"se esperaba EXACTAMENTE 1 construcción de recibo (marcadores {sorted(_RECEIPT_MARKERS)}); hay {len(receipts)} (B331)")  # fmt: skip
    for r in receipts:
        loc = owner.get(id(r), "<módulo>")
        if loc != _EMITTER:
            probs.append(f"el recibo se construye en {loc!r}, no en {_EMITTER!r} (B331)")
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    emitter = funcs.get(_EMITTER)
    if emitter is None:
        probs.append(f"falta la función emisora {_EMITTER!r} (B331)")
    else:
        bad = _params(emitter) & _FORBIDDEN_EMITTER_PARAMS
        if bad:
            probs.append(f"{_EMITTER} no puede aceptar contrato del caller: {sorted(bad)} (B331)")
        calls = {n.func.id for n in ast.walk(emitter) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "load_contract" not in calls:
            probs.append(f"{_EMITTER} debe cargar el contrato canónico por su cuenta (load_contract()) (B331)")
    pure = funcs.get(_PURE)
    if pure is not None and any(_is_receipt(n) for n in ast.walk(pure)):
        probs.append(f"{_PURE}() no puede construir un recibo (debe devolver sólo problemas) (B331)")
    return probs


def _git_tracked(rel: str) -> bool:
    try:
        out = subprocess.run(["git", "ls-files", "--error-unmatch", rel], capture_output=True, text=True)
    except OSError:
        return False
    return out.returncode == 0


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
    probs = problems(src)
    if probs:
        print("✗ autoridad del smoke deep:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ recibo deep construido SÓLO en {_EMITTER} (contrato canónico, sin contrato del caller)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
