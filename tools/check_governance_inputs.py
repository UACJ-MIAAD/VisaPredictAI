#!/usr/bin/env python
"""B286-C: gate POSITIVO de ficheros de ENTRADA de gobernanza.

`security/governance_inputs.json` declara los ficheros que ENCODEAN política de gobernanza — los registries/contratos de
`security/*.{json,yml}`, el workflow `.github/workflows/ci.yml` y `tools/consistency_rules.yml` — con su categoría,
límite de tamaño, formato/parser, consumers exactos y operaciones. Este gate exige una BIYECCIÓN EXACTA:

- todo `.json`/`.yml`/`.yaml` bajo `security/` + `ci.yml` + `consistency_rules.yml` OBSERVADO en el inventario sellado
  está REGISTRADO (y viceversa: sin registros huérfanos);
- cada input registrado es LEGIBLE por `GovernanceSnapshot` con su categoría (O_NOFOLLOW, modo 0644 exacto, uid/nlink) y
  NO excede su `max_bytes`;
- los `consumers` declarados == los ficheros `.py` de producción que REFERENCIAN el path del input (o su basename como
  literal) — un consumer nuevo/faltante rompe la biyección;
- `format` ∈ {json, yaml}, `operations` ⊆ {read}, `reason` no vacía.

Un input nuevo/huérfano/movido, un modo != 0644, un tamaño excedido, o una divergencia de consumers → PROBLEMA
(fail-closed). Sólo stdlib + `GovernanceSnapshot` (lee cada input por UNA observación sellada). NO usa `open()` ad hoc
sobre los inputs ni `git ls-files` directo."""

from __future__ import annotations

import ast
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(
        0, _ROOT
    )  # B286-C: raíz del repo en sys.path para importar `tools.governance_snapshot` en forma script
_REGISTRY = "security/governance_inputs.json"
_SELF = "tools/check_governance_inputs.py"
_SCHEMA_VERSION = 1
_TOP_KEYS = {"schema_version", "note", "required_mode", "inputs"}
_ENTRY_KEYS = {"category", "format", "parser", "consumers", "operations", "max_bytes", "reason", "local_mode"}
_FORMATS = frozenset({"json", "yaml"})
_OPERATIONS = frozenset({"read"})
_CATEGORIES = frozenset({"contract", "authority", "source"})
# universo OBSERVADO de inputs de gobernanza: todo bajo security/ con extensión de datos, + estos dos ficheros nominales.
_SECURITY_PREFIX = "security/"
_DATA_SUFFIXES = (".json", ".yml", ".yaml")
_EXPLICIT = (".github/workflows/ci.yml", "tools/consistency_rules.yml")


def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada: {k!r}")
        seen[k] = v
    return seen


def _observed_inputs(tracked: frozenset[str]) -> set[str]:
    """Del inventario SELLADO: todo `security/*.{json,yml,yaml}` + los ficheros nominales explícitos que existan. El
    propio registro (`_REGISTRY`) NO es un input (es el registro que este gate consume) → se excluye."""
    obs = {r for r in tracked if r.startswith(_SECURITY_PREFIX) and r.endswith(_DATA_SUFFIXES)}
    obs |= {r for r in _EXPLICIT if r in tracked}
    obs.discard(_REGISTRY)
    return obs


def _references_path(tree: ast.AST, rel: str) -> bool:
    """True si el módulo referencia el input por su basename (o path exacto) como SUBSTRING de algún string constante —
    cubre la constante directa (`_WORKFLOW=".github/workflows/ci.yml"`), el `os.path.join(R, "security", "x.json")`, y el
    basename EMBEBIDO en un script-sonda de subproceso (una constante grande que lo contiene). El basename es distintivo;
    una mención en docstring, a lo sumo, exige declararlo (fail-closed seguro)."""
    base = rel.rsplit("/", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if rel in node.value or base in node.value:
                return True
    return False


def problems() -> list[str]:
    from tools.governance_snapshot import GovernanceSnapshot, GovernanceSnapshotError, TrackedQuery

    try:
        with GovernanceSnapshot(_ROOT) as snap:
            reg_raw = snap.read(_REGISTRY, category="contract").data
            try:
                reg = json.loads(reg_raw.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
            except (ValueError, UnicodeDecodeError) as exc:
                return [f"{_REGISTRY}: no-JSON/duplicado ({exc}) (fail-closed B286-C)"]
            if not (isinstance(reg, dict) and set(reg) == _TOP_KEYS):
                return [f"{_REGISTRY}: claves superiores != {sorted(_TOP_KEYS)} (fail-closed)"]
            if not (type(reg["schema_version"]) is int and reg["schema_version"] == _SCHEMA_VERSION):
                return [f"{_REGISTRY}: schema_version != {_SCHEMA_VERSION}"]
            if reg["required_mode"] != "0644":
                return [f"{_REGISTRY}: required_mode != '0644'"]
            declared = reg["inputs"]
            if not isinstance(declared, dict):
                return [f"{_REGISTRY}: 'inputs' no es objeto"]

            tracked = frozenset(snap.tracked(TrackedQuery("suffix", ".py")))
            tracked_all = frozenset(snap.tracked(TrackedQuery("prefix", "security/"))) | frozenset(
                r for r in _EXPLICIT if snap.tracked(TrackedQuery("exact", r))
            )
            observed = _observed_inputs(tracked_all | tracked)
            problems: list[str] = []

            # biyección de la lista de inputs
            for rel in sorted(observed - set(declared)):
                problems.append(f"INPUT DE GOBERNANZA NO REGISTRADO: {rel} (registrar en {_REGISTRY}) (B286-C)")
            for rel in sorted(set(declared) - observed):
                problems.append(
                    f"input REGISTRADO OBSOLETO/MOVIDO: {rel} (ya no existe en el inventario sellado) (B286-C)"
                )

            # cache de fuentes .py para la comprobación de consumers
            src_cache: dict[str, ast.AST | None] = {}

            def _tree(rel_py: str) -> ast.AST | None:
                if rel_py not in src_cache:
                    try:
                        src_cache[rel_py] = ast.parse(snap.read(rel_py, category="source").data, filename=rel_py)
                    except (GovernanceSnapshotError, SyntaxError) as exc:
                        problems.append(f"{rel_py}: no analizable para consumers ({exc}) (fail-closed)")
                        src_cache[rel_py] = None
                return src_cache[rel_py]

            for rel in sorted(observed & set(declared)):
                entry = declared[rel]
                if not (isinstance(entry, dict) and set(entry) == _ENTRY_KEYS):
                    problems.append(f"{_REGISTRY}[{rel}]: claves de entrada != {sorted(_ENTRY_KEYS)}")
                    continue
                if entry["category"] not in _CATEGORIES:
                    problems.append(f"{_REGISTRY}[{rel}]: categoría {entry['category']!r} inválida")
                    continue
                if entry["format"] not in _FORMATS:
                    problems.append(f"{_REGISTRY}[{rel}]: format {entry['format']!r} fuera de {sorted(_FORMATS)}")
                if not (set(entry["operations"]) <= _OPERATIONS and entry["operations"]):
                    problems.append(
                        f"{_REGISTRY}[{rel}]: operations {entry['operations']} fuera de {sorted(_OPERATIONS)}"
                    )
                if not (type(entry["max_bytes"]) is int and entry["max_bytes"] > 0):
                    problems.append(f"{_REGISTRY}[{rel}]: max_bytes inválido")
                for field in ("reason", "parser"):
                    if not (isinstance(entry[field], str) and entry[field].strip()):
                        problems.append(f"{_REGISTRY}[{rel}]: {field} vacío")
                # legibilidad gobernada + tamaño + modo exacto (via la categoría declarada)
                try:
                    data = snap.read(rel, category=entry["category"], max_bytes=entry["max_bytes"]).data
                except GovernanceSnapshotError as exc:
                    problems.append(
                        f"{_REGISTRY}[{rel}]: no legible por GovernanceSnapshot ({exc}) (fail-closed B286-C)"
                    )
                    continue
                # el input parsea con su formato declarado (contrato de forma mínimo)
                if entry["format"] == "json":
                    try:
                        json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        problems.append(f"{_REGISTRY}[{rel}]: no es JSON válido ({exc})")
                # consumers declarados == referencias reales en producción
                declared_consumers = set(entry["consumers"])
                if not (isinstance(entry["consumers"], list) and all(isinstance(c, str) for c in entry["consumers"])):
                    problems.append(f"{_REGISTRY}[{rel}]: consumers no es lista de str")
                    continue
                observed_consumers = set()
                for rel_py in sorted(r for r in tracked if r.startswith("tools/") and r != _SELF):
                    t = _tree(rel_py)
                    if t is not None and _references_path(t, rel):
                        observed_consumers.add(rel_py)
                for c in sorted(declared_consumers - observed_consumers):
                    problems.append(
                        f"{_REGISTRY}[{rel}]: consumer declarado {c} NO referencia el input (obsoleto) (B286-C)"
                    )
                for c in sorted(observed_consumers - declared_consumers):
                    problems.append(
                        f"{_REGISTRY}[{rel}]: {c} referencia el input pero NO está en consumers (registrar) (B286-C)"
                    )
            snap.reverify()
    except (GovernanceSnapshotError, OSError) as exc:
        return [f"gate de inputs de gobernanza fail-closed: {exc}"]
    return problems


def main() -> int:
    probs = problems()
    if probs:
        print("✗ registro de inputs de gobernanza violado:")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"✓ biyección exacta de inputs de gobernanza ({_REGISTRY})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
