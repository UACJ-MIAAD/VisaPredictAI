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
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass

_TARGET = "tools/merge_campaign_pools.py"
_LATCH_METHODS = ("mark_current_certified", "mark_committed_incomplete")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FINGERPRINT_CONTRACT = "security/commit_frontier_fingerprints.json"
# B254/B258: cuerpo crítico pineado, con CONSTANTES DE CÓDIGO (no confiables desde el JSON): fuente exacta, set exacto
# de funciones, schema y algoritmo. El JSON sólo aporta los hashes; todo lo demás debe IGUALAR estas constantes.
_CRITICAL_SOURCE = "tools/campaign_bundle.py"
_CRITICAL_FUNCTIONS = ("commit_current", "_classify_post_authority", "_reconcile_and_raise")
_FINGERPRINT_SCHEMA = 3
_FINGERPRINT_ALGORITHM = "sha256(ast.dump(top_level_FunctionDef,annotate_fields=True,include_attributes=False))"
# B269: además del fingerprint del AST de las 3 funciones, se PINEAN los BYTES EXACTOS del set CERRADO de módulos de
# autoridad — un `commit_current.__code__ = evil.__code__` / alias / decorador / callback / efecto import-time cambia
# los bytes del fichero aunque el nombre y el AST del `def` no cambien. El hash COMPLETO del fichero es la unidad de
# revisión. Frontera honesta: código + contrato pueden cambiar en una PR; el ruleset y la revisión humana lo autorizan.
_AUTHORITY_FILES = ("tools/campaign_bundle.py", "tools/merge_campaign_pools.py", "tools/governed_fs.py", "tools/governed_read.py", "tools/atomic_fs.py")  # fmt: skip
_AUTHORITY_ALGORITHM = "sha256(exact_file_bytes)"
_FINGERPRINT_TOP_KEYS = {"schema_version", "note", "source", "algorithm", "functions", "authority_files_algorithm", "authority_files"}  # fmt: skip
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


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
    # B248/B251: commit_current debe clasificar los fallos POST-autoridad mediante UNA función obligatoria e
    # INCONDICIONAL: `primary = _classify_post_authority(...)` como sentencia de NIVEL DE CUERPO (no dentro de un `if`
    # ni un decoy), ANTES de `quar.close()`, con su resultado REASIGNADO a `primary`. Un `if cert and False: …`, una
    # llamada dentro de una función jamás invocada, o una llamada cuyo resultado se descarta NO satisfacen el contrato.
    cc = funcs.get("commit_current")
    classify_idx: int | None = None
    close_idx: int | None = None
    if cc is not None:
        for i, stmt in enumerate(cc.body):  # SOLO sentencias de nivel de cuerpo (no ast.walk → excluye decoys anidados)
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "primary"
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "_classify_post_authority"
            ):
                classify_idx = i
            if close_idx is None and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "close"
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "quar"
                for c in ast.walk(stmt)
            ):
                close_idx = i
    if classify_idx is None or close_idx is None or classify_idx >= close_idx:
        problems.append("commit_current debe reasignar `primary = _classify_post_authority(...)` INCONDICIONALMENTE (nivel de cuerpo) ANTES de quar.close() (B251)")  # fmt: skip
    elif cc is not None and any(isinstance(cc.body[i], (ast.Raise, ast.Return)) for i in range(classify_idx)):
        # B251 (round 2): un `raise`/`return` de NIVEL DE CUERPO antes del classify lo dejaría INALCANZABLE (decoy)
        problems.append("commit_current tiene un raise/return de nivel de cuerpo ANTES del classify (lo vuelve inalcanzable) (B251)")  # fmt: skip
    # La función obligatoria debe existir y reconciliar CURRENT fd-bound.
    cpa = funcs.get("_classify_post_authority")
    if cpa is None or not any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_reconcile_and_raise"
        for c in ast.walk(cpa)
    ):
        problems.append("_classify_post_authority debe existir y reconciliar CURRENT fd-bound con _reconcile_and_raise (B251)")  # fmt: skip
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


def _iter_bindings(tree: ast.AST):
    """B252: itera `(target_name, value_node)` sobre TODAS las formas de ligadura de un solo nombre:
    `x = v` (Assign), `x: T = v` (AnnAssign), `(x := v)` (NamedExpr) y el desempaquetado `a, b = v1, v2`
    (Assign con Tuple/List balanceado). Base común del análisis de alias por FIXPOINT."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)) and len(node.targets[0].elts) == len(node.value.elts):  # fmt: skip
                for tgt, val in zip(node.targets[0].elts, node.value.elts, strict=True):
                    if isinstance(tgt, ast.Name):
                        yield tgt.id, val
            for tgt in (t for t in node.targets if isinstance(t, ast.Name)):
                yield tgt.id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            yield node.target.id, node.value  # `mod: object = cb`
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            yield node.target.id, node.value  # `(mod := cb)`


def _cb_module_refs(tree: ast.AST) -> tuple[set[str], bool]:
    """B245/B249/B252: `(name_aliases, dotted)` — nombres locales que refieren al MÓDULO `campaign_bundle`
    (`import tools.campaign_bundle as X`, `from tools import campaign_bundle as X`), con PROPAGACIÓN por FIXPOINT
    (no un número fijo de pasadas) sobre TODA forma de ligadura (Assign / AnnAssign `mod: T = cb` / walrus `mod := cb` /
    desempaquetado) y `dotted=True` si el módulo se accede por la ruta punteada `tools.campaign_bundle`. El fixpoint
    termina siempre: `aliases` sólo crece y está acotado por el nº finito de nombres. Cierra `mod = cb; getattr(mod, …)`,
    `mod: object = cb`, `(mod := cb)`, cadenas de alias de cualquier longitud, y `getattr(tools.campaign_bundle, …)`."""
    aliases: set[str] = set()
    dotted = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "tools.campaign_bundle":
                    if a.asname:
                        aliases.add(a.asname)
                    else:
                        dotted = True
        elif isinstance(node, ast.ImportFrom) and node.module in ("tools", None):
            for a in node.names:
                if a.name == "campaign_bundle":
                    aliases.add(a.asname or "campaign_bundle")
    changed = True
    while changed:  # FIXPOINT: repite hasta que `aliases` deja de crecer (cadenas de cualquier longitud)
        changed = False
        for tgt_id, val in _iter_bindings(tree):
            if tgt_id in aliases:
                continue
            # CONSERVADOR (fail-closed): si el VALOR contiene EN CUALQUIER PARTE una ref al módulo de autoridad, el
            # destino PUEDE aliasarlo (`x = cb`, `x = [cb][0]`, `x = (cb,)[i]`, `x = {..: cb}[..]`) → se trata como ref.
            if any(_is_cb_ref(n, aliases, dotted) for n in ast.walk(val)):
                aliases.add(tgt_id)
                changed = True
    return aliases, dotted


_GV_SEEDS = frozenset({"getattr", "vars", "__import__"})
_FACTORY_SEEDS = frozenset({"attrgetter", "methodcaller"})


def _callable_aliases(tree: ast.AST, seeds: frozenset[str]) -> set[str]:
    """B252: nombres locales ligados a uno de los primitivos `seeds` (por Name o por Attribute `mod.<seed>`), por
    FIXPOINT sobre toda forma de ligadura — `g = getattr`, `ag: object = attrgetter`, `(g := getattr)`, y cadenas
    `g = getattr; h = g`. Cierra `g = getattr; g(cb, name)`, `from operator import attrgetter as ag; ag(n)(cb)`, etc."""
    aliases: set[str] = set()
    for node in ast.walk(
        tree
    ):  # semilla desde imports: `from operator import attrgetter as ag`, `from builtins import getattr as g`
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in seeds and a.asname:
                    aliases.add(a.asname)
    changed = True
    while changed:
        changed = False
        for tgt_id, val in _iter_bindings(tree):
            if tgt_id in aliases:
                continue
            if (isinstance(val, ast.Name) and (val.id in seeds or val.id in aliases)) or (isinstance(val, ast.Attribute) and val.attr in seeds):  # fmt: skip
                aliases.add(tgt_id)
                changed = True
    return aliases


def _is_cb_ref(node: ast.AST, aliases: set[str], dotted: bool) -> bool:
    """True si `node` refiere al MÓDULO campaign_bundle: un Name alias o la Attribute `tools.campaign_bundle`."""
    if isinstance(node, ast.Name) and node.id in aliases:
        return True
    return dotted and isinstance(node, ast.Attribute) and node.attr == "campaign_bundle" and isinstance(node.value, ast.Name) and node.value.id == "tools"  # fmt: skip


def _is_getattr_vars_callee(func: ast.AST, reflect: set[str]) -> bool:
    """B252: True si `func` invoca `getattr`/`vars` por NOMBRE directo, por ALIAS reflexivo (`g` con `g = getattr`)
    o por ATRIBUTO (`builtins.getattr`)."""
    if isinstance(func, ast.Name):
        return func.id in ("getattr", "vars") or func.id in reflect
    return isinstance(func, ast.Attribute) and func.attr in ("getattr", "vars")


def _is_attrfactory_callee(func: ast.AST, factory_aliases: set[str]) -> bool:
    """B252: True si `func` es `operator.attrgetter`/`methodcaller` por atributo (`operator.attrgetter`), por nombre
    importado (`from operator import attrgetter`) o por ALIAS de ese nombre (`… as ag`)."""
    if isinstance(func, ast.Attribute):
        return func.attr in ("attrgetter", "methodcaller")
    return isinstance(func, ast.Name) and (func.id in ("attrgetter", "methodcaller") or func.id in factory_aliases)


def _is_partial_callee(func: ast.AST) -> bool:
    """B252 (round 2): True si `func` es `functools.partial` (por atributo) o `partial` (importado)."""
    if isinstance(func, ast.Attribute):
        return func.attr == "partial"
    return isinstance(func, ast.Name) and func.id == "partial"


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
        cb_aliases, cb_dotted = _cb_module_refs(tree)  # B245/B249: refs al módulo campaign_bundle (alias + punteado)
        cb_reflect = _callable_aliases(tree, _GV_SEEDS)  # B252: aliases de getattr/vars/__import__
        cb_factory = _callable_aliases(tree, _FACTORY_SEEDS)  # B252: aliases de attrgetter/methodcaller
        for node in ast.walk(tree):
            if rel in _AUTHORITY_ALLOW and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval", "compile"):  # fmt: skip
                problems.append(f"{rel}:{node.lineno} usa {node.func.id}() en un módulo de autoridad (resolución dinámica prohibida)")  # fmt: skip
            # B245/B249/B252: acceso DINÁMICO a la superficie de autoridad → fail-closed. Cubre `getattr`/`vars` por
            # NOMBRE DIRECTO, por ALIAS reflexivo (`g = getattr; g(cb, n)`) o por ATRIBUTO (`builtins.getattr(cb, n)`);
            # `operator.attrgetter(x)(cb)` / `methodcaller(x)(cb)`; `cb.__dict__`; y la reconstrucción del módulo por
            # `__import__`/`import_module`/`sys.modules`. TODAS restringidas al OPERANDO cbref (o al nombre literal
            # campaign_bundle) → cero falso positivo sobre `getattr(self, campo)` u otras reflexiones no-cb.
            if isinstance(node, ast.Call) and _is_getattr_vars_callee(node.func, cb_reflect) and node.args and _is_cb_ref(node.args[0], cb_aliases, cb_dotted):  # fmt: skip
                fnc = node.func
                gv = fnc.id if isinstance(fnc, ast.Name) else fnc.attr if isinstance(fnc, ast.Attribute) else "?"
                if (
                    gv == "vars" or gv in cb_reflect or len(node.args) < 2 or _const_str(node.args[1]) is None
                ):  # nombre no resoluble / posible `vars`
                    problems.append(f"{rel}:{node.lineno} acceso dinámico ({gv}) sobre el módulo campaign_bundle (fail-closed B245/B249/B252)")  # fmt: skip
            # operator.attrgetter/methodcaller: la fábrica devuelve un callable que se APLICA a cbref → acceso dinámico
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Call) and _is_attrfactory_callee(node.func.func, cb_factory) and node.args and _is_cb_ref(node.args[0], cb_aliases, cb_dotted):  # fmt: skip
                problems.append(f"{rel}:{node.lineno} attrgetter/methodcaller aplicado al módulo campaign_bundle (fail-closed B252)")  # fmt: skip
            # functools.partial capturando reflexión sobre cbref: `partial(getattr, cb)(name)` = `getattr(cb, name)`.
            # Forma 1 — el propio partial captura una primitiva reflexiva Y un cbref en sus argumentos.
            if isinstance(node, ast.Call) and _is_partial_callee(node.func) and any(_is_getattr_vars_callee(a, cb_reflect) for a in node.args) and any(_is_cb_ref(a, cb_aliases, cb_dotted) for a in node.args):  # fmt: skip
                problems.append(f"{rel}:{node.lineno} functools.partial captura reflexión sobre campaign_bundle (fail-closed B252)")  # fmt: skip
            # Forma 2 — `partial(getattr)(cb, name)`: el partial de una primitiva reflexiva se APLICA a cbref.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Call) and _is_partial_callee(node.func.func) and any(_is_getattr_vars_callee(a, cb_reflect) for a in node.func.args) and node.args and _is_cb_ref(node.args[0], cb_aliases, cb_dotted):  # fmt: skip
                problems.append(f"{rel}:{node.lineno} partial de reflexión aplicado al módulo campaign_bundle (fail-closed B252)")  # fmt: skip
            if isinstance(node, ast.Attribute) and node.attr == "__dict__" and _is_cb_ref(node.value, cb_aliases, cb_dotted):  # fmt: skip
                problems.append(f"{rel}:{node.lineno} accede a campaign_bundle.__dict__ (elude el gate B245/B249)")
            # __import__('...campaign_bundle...') reconstruye el módulo de autoridad por nombre (builtin o alias reflexivo)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and (node.func.id == "__import__" or node.func.id in cb_reflect) and node.args and (_ci := _const_str(node.args[0])) and "campaign_bundle" in _ci:  # fmt: skip
                problems.append(f"{rel}:{node.lineno} __import__ de campaign_bundle (elude el gate B252)")
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


def _fn_fingerprint(node: ast.FunctionDef) -> str:
    """B254: fingerprint del AST NORMALIZADO de una función (sin atributos de posición) — insensible a
    formato/comentarios/whitespace, sensible a CUALQUIER cambio estructural (`if True: raise`, `while True`,
    un return/raise que vuelva inalcanzable un paso, reordenar, cambiar una llamada)."""
    return hashlib.sha256(ast.dump(node, annotate_fields=True, include_attributes=False).encode()).hexdigest()


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def _fingerprint_contract_problems(contract: object) -> list[str]:
    """B258: valida el ESQUEMA del contrato con constantes de código (no confía nada del JSON salvo los hashes):
    claves superiores exactas, `schema_version is 2` (bool≠int), `source`/`algorithm` == constantes, set EXACTO de las
    3 funciones, cada hash `[0-9a-f]{64}`."""
    if not isinstance(contract, dict) or set(contract) != _FINGERPRINT_TOP_KEYS:
        return [f"{_FINGERPRINT_CONTRACT}: claves superiores != {sorted(_FINGERPRINT_TOP_KEYS)} (fail-closed B258)"]
    if not (type(contract["schema_version"]) is int and contract["schema_version"] == _FINGERPRINT_SCHEMA):
        return [f"{_FINGERPRINT_CONTRACT}: schema_version no es el entero {_FINGERPRINT_SCHEMA} (B258)"]
    if contract["source"] != _CRITICAL_SOURCE:
        return [f"{_FINGERPRINT_CONTRACT}: source != {_CRITICAL_SOURCE!r} (B258)"]
    if contract["algorithm"] != _FINGERPRINT_ALGORITHM:
        return [f"{_FINGERPRINT_CONTRACT}: algorithm != la constante de código (B258)"]
    funcs = contract["functions"]
    if not isinstance(funcs, dict) or set(funcs) != set(_CRITICAL_FUNCTIONS):
        return [f"{_FINGERPRINT_CONTRACT}: functions != EXACTAMENTE {list(_CRITICAL_FUNCTIONS)} (B258)"]
    if not all(isinstance(v, str) and _HEX64.match(v) for v in funcs.values()):
        return [f"{_FINGERPRINT_CONTRACT}: algún hash no es [0-9a-f]{{64}} (B258)"]
    if contract["authority_files_algorithm"] != _AUTHORITY_ALGORITHM:
        return [f"{_FINGERPRINT_CONTRACT}: authority_files_algorithm != la constante de código (B269)"]
    af = contract["authority_files"]
    if not isinstance(af, dict) or set(af) != set(_AUTHORITY_FILES):
        return [f"{_FINGERPRINT_CONTRACT}: authority_files != EXACTAMENTE {list(_AUTHORITY_FILES)} (B269)"]
    if not all(isinstance(v, str) and _HEX64.match(v) for v in af.values()):
        return [f"{_FINGERPRINT_CONTRACT}: algún hash de authority_files no es [0-9a-f]{{64}} (B269)"]
    return []


@dataclass(frozen=True)
class _GovernedBytes:
    """B274: bytes de un fichero versionado leídos por una cadena GOBERNADA (openat componente a componente + O_NOFOLLOW
    + fstat regular/uid/nlink/modo exacto + snapshot pre/post + revalidación nombre↔inode). Los bytes NO se reabren por
    ruta; el resto de la certificación (hash, JSON, AST) se hace sobre `data`."""

    data: bytes
    rel: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    uid: int
    nlink: int


def _governed_rel_parts(rel: str) -> list[str] | None:
    """B274: `rel` debe ser una ruta POSIX relativa SIMPLE y cerrada: sin NUL, no absoluta, sin `.`/`..`, sin componentes vacíos."""  # fmt: skip
    if not rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts


def _read_governed_repo_file(rel: str, *, exact_mode: int = 0o644) -> tuple[_GovernedBytes | None, list[str]]:
    """B274: lee los bytes de un fichero versionado del repo por una cadena GOBERNADA, stdlib-only, SIN importar ninguno
    de los cinco módulos cuyos bytes certifica. Abre `_ROOT` como ancla y desciende componente a componente con
    `O_DIRECTORY|O_NOFOLLOW` (ningún ancestro puede ser symlink); el leaf con `O_RDONLY|O_NOFOLLOW|O_NONBLOCK` (un FIFO
    sin escritor NO cuelga antes del `fstat`). Exige regular, uid actual, `nlink==1`, modo EXACTO y sin bits especiales;
    lee SÓLO de ese fd (jamás reabre por ruta); toma `fstat` snapshot pre/post idéntico; revalida nombre↔inode de cada
    componente y del leaf; un cierre fallido invalida el resultado. Frontera honesta: evita symlinks, objetos especiales,
    rebind visible y mutación del inode DURANTE la lectura; NO es una instantánea criptográfica contra un proceso hostil
    del mismo UID que alterne y restaure todo el árbol entre checkpoints — la autoridad final sigue siendo ruleset +
    revisión del diff."""
    parts = _governed_rel_parts(rel)
    if parts is None:
        return None, [f"{rel}: ruta relativa POSIX inválida (fail-closed B274)"]
    dir_fds: list[int] = []
    ancestors: list[tuple[str, int, os.stat_result]] = []  # (nombre, parent_fd, fstat) para revalidar nombre↔inode
    try:
        try:
            root_fd = os.open(_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            return None, [f"{rel}: raíz {_ROOT!r} no abrible como directorio ({exc}) (fail-closed B274)"]
        dir_fds.append(root_fd)
        cur = root_fd
        for comp in parts[:-1]:
            try:
                nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur)
            except OSError as exc:
                return None, [f"{rel}: componente {comp!r} no es directorio no-symlink abrible ({exc}) (fail-closed B274)"]  # fmt: skip
            ancestors.append((comp, cur, os.fstat(nfd)))
            dir_fds.append(nfd)
            cur = nfd
        leaf = parts[-1]
        try:
            lfd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=cur)
        except OSError as exc:
            return None, [f"{rel}: leaf {leaf!r} no abrible sin seguir symlink ({exc}) (fail-closed B274)"]
        close_problem: str | None = None
        result: tuple[_GovernedBytes | None, list[str]] | None = None
        try:
            st0 = os.fstat(lfd)
            if not stat.S_ISREG(st0.st_mode):
                return None, [f"{rel}: no es un fichero regular (fail-closed B274)"]
            if st0.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                return None, [f"{rel}: bits especiales setuid/setgid/sticky (fail-closed B274)"]
            if stat.S_IMODE(st0.st_mode) != exact_mode:
                return None, [f"{rel}: modo {oct(stat.S_IMODE(st0.st_mode))} != {oct(exact_mode)} exacto (fail-closed B274)"]  # fmt: skip
            if st0.st_uid != os.getuid():
                return None, [f"{rel}: uid {st0.st_uid} != {os.getuid()} actual (fail-closed B274)"]
            if st0.st_nlink != 1:
                return None, [f"{rel}: nlink {st0.st_nlink} != 1 (hardlink) (fail-closed B274)"]
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = os.read(lfd, 1 << 16)
                except OSError as exc:
                    return None, [f"{rel}: error de lectura ({exc}) (fail-closed B274)"]
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            st1 = os.fstat(lfd)
            snap0 = (st0.st_dev, st0.st_ino, st0.st_size, st0.st_mtime_ns, st0.st_ctime_ns, st0.st_mode, st0.st_uid, st0.st_nlink)  # fmt: skip
            snap1 = (st1.st_dev, st1.st_ino, st1.st_size, st1.st_mtime_ns, st1.st_ctime_ns, st1.st_mode, st1.st_uid, st1.st_nlink)  # fmt: skip
            if snap0 != snap1:
                return None, [f"{rel}: el inode del leaf cambió durante la lectura (fail-closed B274)"]
            if len(data) != st0.st_size:
                return None, [f"{rel}: tamaño leído {len(data)} != fstat {st0.st_size} (fail-closed B274)"]
            result = (
                _GovernedBytes(data=data, rel=rel, dev=st0.st_dev, ino=st0.st_ino, size=st0.st_size, mtime_ns=st0.st_mtime_ns, ctime_ns=st0.st_ctime_ns, mode=st0.st_mode, uid=st0.st_uid, nlink=st0.st_nlink),  # fmt: skip
                [],
            )
        finally:
            try:
                os.close(lfd)
            except OSError as exc:
                close_problem = f"{rel}: fallo al cerrar el leaf ({exc}) (fail-closed B274)"
        if close_problem is not None:
            return None, [close_problem]
        assert result is not None
        gb, _ = result
        for name, pfd, fst in ancestors:  # revalidar nombre↔inode de cada ancestro y del leaf
            try:
                by_name = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            except OSError as exc:
                return None, [f"{rel}: ancestro {name!r} no re-stat-able ({exc}) (fail-closed B274)"]
            if (by_name.st_dev, by_name.st_ino) != (fst.st_dev, fst.st_ino):
                return None, [f"{rel}: el ancestro {name!r} cambió de inode durante la lectura (fail-closed B274)"]
        try:
            leaf_by_name = os.stat(leaf, dir_fd=cur, follow_symlinks=False)
        except OSError as exc:
            return None, [f"{rel}: leaf {leaf!r} no re-stat-able ({exc}) (fail-closed B274)"]
        if (leaf_by_name.st_dev, leaf_by_name.st_ino) != (gb.dev, gb.ino):
            return None, [f"{rel}: el leaf {leaf!r} cambió de inode durante la lectura (fail-closed B274)"]
        return result
    finally:
        for fd in reversed(dir_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _read_authority_files(contract: dict) -> tuple[dict[str, _GovernedBytes], list[str]]:
    """B269/B274: lee por cadena GOBERNADA los bytes exactos del set cerrado de módulos de autoridad y verifica su
    `sha256` contra el contrato. Devuelve `(governed, problemas)`; `governed[rel]` retiene los bytes ya certificados para
    reusarlos sin una segunda apertura (p. ej. el AST del `_CRITICAL_SOURCE`). Cualquier byte nuevo (incl. un
    `commit_current.__code__ = …`, alias, decorador, callback o efecto import-time) rompe el hash del fichero completo."""
    af = contract["authority_files"]
    governed: dict[str, _GovernedBytes] = {}
    problems: list[str] = []
    for rel in _AUTHORITY_FILES:
        if not _git_tracked(rel):
            problems.append(f"{rel}: NO versionado (fail-closed B269)")
            continue
        gb, gprobs = _read_governed_repo_file(rel)
        if gb is None:
            problems.extend(gprobs)
            continue
        governed[rel] = gb
        if hashlib.sha256(gb.data).hexdigest() != af[rel]:
            problems.append(f"{rel}: los bytes cambiaron (sha != contrato) → actualizar {_FINGERPRINT_CONTRACT} + re-revisar el fichero completo y re-correr la batería (B269)")  # fmt: skip
    return governed, problems


def _authority_files_problems(contract: dict) -> list[str]:
    """B269: envoltorio de compatibilidad — sólo los problemas de la lectura gobernada de autoridad (ver `_read_authority_files`)."""  # fmt: skip
    return _read_authority_files(contract)[1]


def _critical_defs_problems(tree: ast.Module) -> tuple[dict[str, ast.FunctionDef], list[str]]:
    """B258: selecciona las funciones críticas SÓLO a nivel GLOBAL (`tree.body`), exactamente una por nombre. Rechaza
    toda definición homónima ANIDADA (en función/clase — posible decoy) y `async def` para estos nombres. Devuelve
    `(defs_globales, problemas)`."""
    problems: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name in _CRITICAL_FUNCTIONS:
            problems.append(f"{_CRITICAL_SOURCE}: {n.name} definida como `async def` (no permitido para funciones críticas) (B258)")  # fmt: skip
        elif isinstance(n, ast.FunctionDef) and n.name in _CRITICAL_FUNCTIONS and n not in tree.body:
            problems.append(f"{_CRITICAL_SOURCE}: {n.name} definida ANIDADA (posible decoy) — sólo se permite a nivel global (B258)")  # fmt: skip
    top: dict[str, list[ast.FunctionDef]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in _CRITICAL_FUNCTIONS:
            top.setdefault(stmt.name, []).append(stmt)
    defs: dict[str, ast.FunctionDef] = {}
    for name in _CRITICAL_FUNCTIONS:
        cand = top.get(name, [])
        if len(cand) != 1:
            problems.append(f"{_CRITICAL_SOURCE}: {name} tiene {len(cand)} definiciones GLOBALES (debe ser exactamente 1) (B258)")  # fmt: skip
        else:
            defs[name] = cand[0]
    return defs, problems


_MODULE_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)  # fmt: skip


def _enclosing_scope(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    """Ancestro de scope más cercano (función/lambda/clase/comprehension) de `node`; None = scope de MÓDULO."""
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, _MODULE_SCOPES):
            return cur
        cur = parents.get(id(cur))
    return None


def _critical_binding_problems(tree: ast.Module) -> list[str]:
    """B264: el fingerprint pinea el AST del `def`, pero Python exporta el BINDING GLOBAL — que puede re-ligarse o
    borrarse después del `def`. Certifica que la ÚNICA escritura de cada nombre crítico a nivel módulo es su `def`:
    rechaza cualquier otro binding/borrado de nivel módulo (Assign/AnnAssign/AugAssign/walrus/for/with/except/match/
    Import/ImportFrom/ClassDef), `del`, `import *`, y todo `global <crítico>` (un escritor dentro de una función). La
    mutación DINÁMICA (globals()/setattr/exec) la caza el gate de reflexión (primitivos no registrados). Fail-closed."""
    crit = set(_CRITICAL_FUNCTIONS)
    problems: list[str] = []
    parents: dict[int, ast.AST] = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[id(c)] = n
    # funciones que declaran `global <crítico>` (podrían escribir el binding del módulo)
    global_writers: dict[int, set[str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Global):
            gnames = set(n.names) & crit
            if gnames:
                fn = _enclosing_scope(n, parents)
                while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = _enclosing_scope(fn, parents)
                if fn is not None:
                    global_writers.setdefault(id(fn), set()).update(gnames)
                problems.append(f"{_CRITICAL_SOURCE}: `global {', '.join(sorted(gnames))}` — un escritor del binding crítico fuera del def (B264)")  # fmt: skip

    def _binds_module(node: ast.AST, name: str) -> bool:
        scope = _enclosing_scope(node, parents)
        if scope is None:
            return True  # scope de módulo
        return isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and name in global_writers.get(id(scope), set())  # fmt: skip

    for n in ast.walk(tree):
        # Store/Del de un Name crítico (Assign/AnnAssign/AugAssign/walrus/for/with/tuple-unpack targets)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id in crit and _binds_module(n, n.id):  # fmt: skip
            verb = "borra" if isinstance(n.ctx, ast.Del) else "re-liga"
            problems.append(f"{_CRITICAL_SOURCE}: algo {verb} el binding global `{n.id}` (sólo el def puede crearlo) (B264)")  # fmt: skip
        elif isinstance(n, ast.ClassDef) and n.name in crit and _binds_module(n, n.name):
            problems.append(f"{_CRITICAL_SOURCE}: `class {n.name}` re-liga el nombre crítico (B264)")
        elif isinstance(n, ast.ExceptHandler) and n.name in crit and _binds_module(n, n.name):
            problems.append(f"{_CRITICAL_SOURCE}: `except … as {n.name}` re-liga el binding crítico (B264)")
        elif isinstance(n, ast.MatchAs) and n.name in crit and _binds_module(n, n.name):
            problems.append(f"{_CRITICAL_SOURCE}: pattern `as {n.name}` re-liga el binding crítico (B264)")
        elif isinstance(n, ast.MatchStar) and n.name in crit and _binds_module(n, n.name):
            problems.append(f"{_CRITICAL_SOURCE}: pattern `*{n.name}` re-liga el binding crítico (B264)")
        elif isinstance(n, ast.MatchMapping) and n.rest in crit and _binds_module(n, n.rest):
            problems.append(f"{_CRITICAL_SOURCE}: pattern `**{n.rest}` re-liga el binding crítico (B264)")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if (a.asname or a.name.split(".")[0]) in crit and _binds_module(n, a.asname or a.name):
                    problems.append(f"{_CRITICAL_SOURCE}: `import` re-liga el nombre crítico (B264)")
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "*":
                    problems.append(f"{_CRITICAL_SOURCE}: `import *` puede re-ligar bindings críticos (B264)")
                elif (a.asname or a.name) in crit and _binds_module(n, a.asname or a.name):
                    problems.append(f"{_CRITICAL_SOURCE}: `from … import … as {a.asname or a.name}` re-liga el binding crítico (B264)")  # fmt: skip
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("globals", "locals", "vars", "exec", "eval", "setattr"):  # fmt: skip
            problems.append(f"{_CRITICAL_SOURCE}: `{n.func.id}(...)` puede mutar bindings del módulo — PROHIBIDO en la fuente de autoridad, no allowlisted (B264)")  # fmt: skip
    return problems


def fingerprint_problems() -> list[str]:
    """B254/B258: PINEA el AST GLOBAL del cuerpo crítico de la frontera de commit (`commit_current`,
    `_classify_post_authority`, `_reconcile_and_raise`) contra `security/commit_frontier_fingerprints.json`. El gate NO
    infiere alcanzabilidad con reglas parciales; exige que el AST del `FunctionDef` de NIVEL GLOBAL sea EXACTAMENTE el
    revisado, seleccionado SÓLO de `tree.body` (nunca `ast.walk`, que dejaba a un decoy anidado homónimo ganar la clave,
    B258). El esquema se valida con constantes de código (fuente/algoritmo/set de funciones); el JSON sólo aporta los
    hashes. Cualquier divergencia obliga a actualizar el hash Y re-correr la batería adversarial + los tests de
    comportamiento (que son, junto al fingerprint, el contrato). Fail-closed en cada paso."""
    if not _git_tracked(_FINGERPRINT_CONTRACT):
        return [f"{_FINGERPRINT_CONTRACT}: NO versionado (fail-closed B254/B274)"]
    gov_contract, cprobs = _read_governed_repo_file(_FINGERPRINT_CONTRACT)  # B274: lectura gobernada del contrato
    if gov_contract is None:
        return cprobs
    try:
        contract = json.loads(gov_contract.data.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except (ValueError, UnicodeDecodeError) as exc:
        return [f"{_FINGERPRINT_CONTRACT}: no-JSON/duplicado/no-utf8 ({exc}) (fail-closed B254/B258/B274)"]
    schema_probs = _fingerprint_contract_problems(contract)
    if schema_probs:
        return schema_probs
    funcs = contract["functions"]
    # B274: lee por cadena gobernada los 5 módulos de autoridad (B269) y REUSA esos bytes para el AST del crítico —
    # `_CRITICAL_SOURCE` es uno de ellos, así que no hay una segunda apertura por ruta.
    governed, problems = _read_authority_files(contract)
    if not _git_tracked(_CRITICAL_SOURCE):
        return problems + [f"{_CRITICAL_SOURCE}: NO versionado (fail-closed B254)"]
    crit_gb = governed.get(_CRITICAL_SOURCE)
    if crit_gb is None:
        return problems  # la lectura gobernada del crítico ya falló y está reportada
    try:
        tree = ast.parse(crit_gb.data)
    except SyntaxError as exc:
        return problems + [f"{_CRITICAL_SOURCE}: no parseable ({exc}) (fail-closed B254)"]
    defs, dprobs = _critical_defs_problems(tree)
    problems += dprobs
    problems += _critical_binding_problems(tree)  # B264: el binding global no puede re-ligarse/borrarse tras el def
    for name in _CRITICAL_FUNCTIONS:
        node = defs.get(name)
        if node is None:
            continue  # ya reportado por _critical_defs_problems (0 o >1 definiciones globales)
        got = _fn_fingerprint(node)
        if got != funcs[name]:
            problems.append(f"{_CRITICAL_SOURCE}: el AST GLOBAL de {name!r} cambió (fingerprint {got[:12]}… != {str(funcs[name])[:12]}…); actualizar {_FINGERPRINT_CONTRACT} + re-correr la batería adversarial y los tests de comportamiento (B254/B258)")  # fmt: skip
    return problems


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
    problems += fingerprint_problems()  # B254: PIN del AST del cuerpo crítico (runtime + fingerprint = el contrato)
    if problems:
        print("✗ frontera de commit violada:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ frontera de commit íntegra (autoridad = CommitCertificate de CURRENT): {_TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
