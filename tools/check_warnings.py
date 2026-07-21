#!/usr/bin/env python
"""Fase 4 (P0R.5): gate del CONTRATO de warnings.

La suite corre con `filterwarnings = ["error", <ignores estrechos>]` aplicados desde `tests/conftest.py` (la constante
`FILTERWARNINGS` + `pytest_configure`; en el conftest, NO en pyproject, para dejar pyproject.toml byte-idéntico y no tocar
el manifiesto de locks). Todo warning es un ERROR salvo un conjunto pequeño de warnings UPSTREAM/experimentales
inevitables, cada uno ignorado por un filtro ESTRECHO (message-prefix + categoría) y documentado con expiry en
`security/warnings_registry.json`. Este gate exige:

- el PRIMER filtro es exactamente `error` (sin él, `error` no aplica y la supresión no tiene sentido);
- NINGÚN filtro global amplio (`ignore::Warning`, `ignore` sin mensaje, `default`/`always` amplios);
- BIYECCIÓN EXACTA registro ⇔ filtros `ignore:` de conftest: cada entrada del registro deriva EXACTAMENTE un filtro
  `ignore:<message_prefix>:<category>` presente, y ningún filtro `ignore:` carece de entrada;
- cada entrada con esquema exacto (package/version/category/message_prefix/origin/reason/issue/review), `category`
  bien formada como nombre de `Warning` (la existencia real la impone pytest al colectar), `review` fecha ISO NO expirada.

Sólo stdlib (`ast`/`json`/`re`/`datetime`); NO import dinámico (prohibido por el gate de reflexión). Fail-closed."""

from __future__ import annotations

import ast
import datetime
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REGISTRY = "security/warnings_registry.json"
# Los filtros VIVEN en tests/conftest.py (constante FILTERWARNINGS + pytest_configure), NO en pyproject.toml — así
# pyproject queda byte-idéntico y no se toca el manifiesto de locks. El gate lee esa lista por AST.
_CONFTEST = "tests/conftest.py"
_FW_CONST = "FILTERWARNINGS"
_SCHEMA_VERSION = 1
_TOP_KEYS = {"schema_version", "note", "warnings"}
_ENTRY_KEYS = {"id", "package", "version", "category", "message_prefix", "origin", "reason", "issue", "review"}
# formas de supresión PROHIBIDAS (amplias): matchean cualquier filtro que suprima sin mensaje o por categoría raíz.
_BROAD_FORBIDDEN = ("ignore::Warning", "ignore:::", "always", "default", "module", "once")
# categorías builtin de Warning aceptadas por nombre simple; el resto DEBE ser un path punteado cuyo último componente
# termine en "Warning". La resolubilidad REAL de la clase la impone pytest al parsear `filterwarnings` (una categoría
# inexistente ROMPE la colección de la suite) — el gate NO importa dinámicamente (prohibido por el gate de reflexión).
_BUILTIN_WARNINGS = frozenset({"Warning", "UserWarning", "DeprecationWarning", "PendingDeprecationWarning", "SyntaxWarning", "RuntimeWarning", "FutureWarning", "ImportWarning", "UnicodeWarning", "BytesWarning", "ResourceWarning", "EncodingWarning"})  # fmt: skip
_DOTTED_CATEGORY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _category_wellformed(cat: object) -> bool:
    """Estáticamente: builtin de Warning por nombre simple, o path punteado cuyo último componente termina en 'Warning'.
    (La existencia real de la clase la fuerza pytest al resolver el filtro; el gate no hace import dinámico.)"""
    if not isinstance(cat, str) or not cat:
        return False
    if cat in _BUILTIN_WARNINGS:
        return True
    return bool(_DOTTED_CATEGORY.fullmatch(cat)) and cat.rsplit(".", 1)[-1].endswith("Warning")


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def _derived_filter(entry: dict) -> str:
    return f"ignore:{entry['message_prefix']}:{entry['category']}"


def _conftest_filterwarnings() -> tuple[list[str] | None, str | None]:
    """Extrae la lista de strings `FILTERWARNINGS = [...]` de `tests/conftest.py` por AST (fail-closed si falta/impura)."""
    try:
        with open(os.path.join(_ROOT, _CONFTEST), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=_CONFTEST)
    except (OSError, SyntaxError) as exc:
        return None, f"{_CONFTEST}: ilegible/no parseable ({exc}) (fail-closed)"
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == _FW_CONST for t in node.targets):
            continue
        if not isinstance(node.value, ast.List):
            return None, f"{_CONFTEST}: {_FW_CONST} no es una lista literal (fail-closed)"
        vals: list[str] = []
        for el in node.value.elts:
            if not (isinstance(el, ast.Constant) and isinstance(el.value, str)):
                return None, f"{_CONFTEST}: {_FW_CONST} tiene un elemento no-string literal (fail-closed)"
            vals.append(el.value)
        return vals, None
    return None, f"{_CONFTEST}: no define {_FW_CONST} (fail-closed)"


def problems() -> list[str]:
    problems: list[str] = []
    # 1) registro
    try:
        with open(os.path.join(_ROOT, _REGISTRY), encoding="utf-8") as fh:
            reg = json.loads(fh.read(), object_pairs_hook=_no_dup_pairs)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [f"{_REGISTRY}: ilegible/no-JSON/duplicado ({exc}) (fail-closed)"]
    if not (isinstance(reg, dict) and set(reg) == _TOP_KEYS):
        return [f"{_REGISTRY}: claves superiores != {sorted(_TOP_KEYS)} (fail-closed)"]
    if not (type(reg["schema_version"]) is int and reg["schema_version"] == _SCHEMA_VERSION):
        return [f"{_REGISTRY}: schema_version != {_SCHEMA_VERSION}"]
    if not isinstance(reg["warnings"], list):
        return [f"{_REGISTRY}: 'warnings' no es lista"]

    today = datetime.date.today()
    derived: list[str] = []
    seen_ids: set[str] = set()
    for e in reg["warnings"]:
        if not (isinstance(e, dict) and set(e) == _ENTRY_KEYS):
            problems.append(f"{_REGISTRY}: entrada con claves != {sorted(_ENTRY_KEYS)}: {e!r}")
            continue
        if e["id"] in seen_ids:
            problems.append(f"{_REGISTRY}: id duplicado {e['id']!r}")
        seen_ids.add(e["id"])
        for field in ("id", "package", "version", "message_prefix", "origin", "reason", "issue"):
            if not (isinstance(e[field], str) and e[field].strip()):
                problems.append(f"{_REGISTRY}[{e['id']}]: {field} vacío")
        # categoría bien formada (builtin de Warning o path punteado terminado en 'Warning'); pytest impone la existencia
        if not _category_wellformed(e["category"]):
            problems.append(
                f"{_REGISTRY}[{e['id']}]: category {e['category']!r} no es un nombre de Warning bien formado"
            )
        # expiry
        try:
            rev = datetime.date.fromisoformat(e["review"]) if isinstance(e["review"], str) else None
        except ValueError:
            rev = None
        if rev is None:
            problems.append(f"{_REGISTRY}[{e['id']}]: review no es fecha ISO")
        elif rev < today:
            problems.append(f"{_REGISTRY}[{e['id']}]: review {e['review']} EXPIRADO — re-evaluar el warning (Fase 4)")
        derived.append(_derived_filter(e))

    # 2) filtros de conftest.py
    fw, ferr = _conftest_filterwarnings()
    if ferr is not None or fw is None:
        return problems + [ferr or f"{_CONFTEST}: sin filtros (Fase 4)"]
    if not fw:
        return problems + [f"{_CONFTEST}: {_FW_CONST} vacío (Fase 4)"]
    if fw[0] != "error":
        problems.append(f"{_CONFTEST}: el PRIMER {_FW_CONST} debe ser 'error' (obtenido {fw[0]!r}) (Fase 4)")
    for f in fw[1:]:
        if not (f.startswith("ignore:") and f.count(":") >= 2 and f.split(":", 2)[1].strip()):
            problems.append(f"{_CONFTEST}: filtro {f!r} no es un `ignore:<mensaje>:<categoría>` estrecho (Fase 4)")
        if any(bad in f for bad in _BROAD_FORBIDDEN) or f in _BROAD_FORBIDDEN:
            problems.append(f"{_CONFTEST}: filtro amplio PROHIBIDO {f!r} (sin supresión global) (Fase 4)")

    # 3) biyección registro <-> filtros ignore de conftest
    ignores = [f for f in fw if f.startswith("ignore:")]
    reg_set, pp_set = set(derived), set(ignores)
    for f in sorted(reg_set - pp_set):
        problems.append(f"registro sin filtro en conftest: {f!r} (añadir a {_FW_CONST}) (Fase 4)")
    for f in sorted(pp_set - reg_set):
        problems.append(f"filtro `ignore:` sin entrada en el registro: {f!r} (registrar o quitar) (Fase 4)")
    return problems


def main() -> int:
    probs = problems()
    if probs:
        print("✗ contrato de warnings violado:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ warnings como contrato: `error` global + filtros estrechos en biyección con {_REGISTRY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
