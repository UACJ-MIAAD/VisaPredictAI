#!/usr/bin/env python
"""B331/B335: gate AST POSITIVO de la AUTORIDAD del smoke deep (`tools/deep_smoke.py`).

`evaluate()` construía el recibo con un `DeepSmokeContract` del CALLER (B331) y `certify_observation(lock_rel, observation)`
lo aceptaba de un `DeepObservation` construible directamente por el caller (B335) — inventario/orígenes/commit fabricados
producían recibo verde. Este gate FAIL-CLOSED fija por estructura que:

1. Existe EXACTAMENTE UNA construcción del literal del recibo (identificada por sus claves marcadoras) y vive DENTRO del
   emisor `certify_runtime` — ninguna otra función lo construye.
2. `certify_runtime` acepta SÓLO `lock_rel` (ni observación ni contrato del caller) y llama `load_contract()` +
   `observe_runtime()` por su cuenta.
3. NINGUNA función que RECIBA un `DeepObservation` construye recibo (si no, el caller podría reinyectar).
4. `run` sólo DELEGA a `certify_runtime` y no construye recibo.
5. La función PURA `evaluate` NO construye recibo (sólo devuelve problemas).

Escanea SÓLO el fichero versionado `tools/deep_smoke.py`; si git falla o el fichero no parsea, FALLA cerrado. El núcleo
`problems(src)` es PURO/testeable con fuente inyectada (para regresiones adversariales)."""

from __future__ import annotations

import ast
import subprocess
import sys

_TARGET = "tools/deep_smoke.py"
_EMITTER = "certify_runtime"  # B335: emisor SIN parámetro de observación (observa por su cuenta)
_PURE = "evaluate"
_DELEGATOR = "run"
_OBSERVATION_TYPE = "DeepObservation"
# Claves MARCADORAS que identifican inequívocamente el literal del recibo (no una config cualquiera).
_RECEIPT_MARKERS = frozenset({"deep_smoke_contract_sha256", "tensor_checksum", "lock_sha256"})
_EMITTER_PARAMS = frozenset(
    {"lock_rel"}
)  # B335: el emisor acepta SÓLO lock_rel — ni observación ni contrato del caller
_EMITTER_REQUIRED_CALLS = ("load_contract", "observe_runtime")  # B335: observa y carga el contrato por su cuenta


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


def _takes_observation(fn: ast.FunctionDef) -> bool:
    """True si alguna anotación de parámetro es `DeepObservation` (bare o `ds.DeepObservation`)."""
    for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs):
        ann = a.annotation
        if isinstance(ann, ast.Name) and ann.id == _OBSERVATION_TYPE:
            return True
        if isinstance(ann, ast.Attribute) and ann.attr == _OBSERVATION_TYPE:
            return True
    return False


def _named_calls(fn: ast.FunctionDef) -> set[str]:
    return {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


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
            probs.append(f"el recibo se construye en {loc!r}, no en {_EMITTER!r} (B331/B335)")
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    emitter = funcs.get(_EMITTER)
    if emitter is None:
        probs.append(f"falta la función emisora {_EMITTER!r} (B335)")
    else:
        if _params(emitter) != _EMITTER_PARAMS:  # B335: SÓLO lock_rel — jamás una observación/contrato del caller
            probs.append(f"{_EMITTER} debe aceptar SÓLO {sorted(_EMITTER_PARAMS)} (tiene {sorted(_params(emitter))}) (B335)")  # fmt: skip
        calls = _named_calls(emitter)
        for req in _EMITTER_REQUIRED_CALLS:  # observa y carga el contrato por su cuenta
            if req not in calls:
                probs.append(f"{_EMITTER} debe llamar {req}() por su cuenta (B335)")
    # B335: NINGUNA función que RECIBA un DeepObservation puede construir un recibo (cerraría de nuevo la inyección).
    for name, fn in funcs.items():
        if _takes_observation(fn) and any(_is_receipt(n) for n in ast.walk(fn)):
            probs.append(f"{name} recibe {_OBSERVATION_TYPE} y construye recibo — el caller podría inyectar (B335)")
    delegator = funcs.get(_DELEGATOR)  # `run` sólo delega al emisor y no construye recibo
    if delegator is not None:
        if _EMITTER not in _named_calls(delegator):
            probs.append(f"{_DELEGATOR} debe delegar a {_EMITTER} (B335)")
        if any(_is_receipt(n) for n in ast.walk(delegator)):
            probs.append(f"{_DELEGATOR} no puede construir un recibo (debe delegar) (B335)")
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
