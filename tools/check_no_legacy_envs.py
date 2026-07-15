#!/usr/bin/env python
"""Gate anti-entornos legacy (P0R.5, C4/B2). El call graph oficial debe MIGRAR de `ante/bin/*` y
`ante_nf/bin/*` a la interfaz gobernada `python -m tools.python_env run-python --profile <P> [--variant V]`.
Este gate es un TRINQUETE: cuenta las referencias a los intérpretes legacy por fichero y las compara con
`docs/legacy_env_baseline.json`. Cualquier fichero que SUPERE su baseline (uso legacy NUEVO) FALLA; bajar
el baseline (migrar) es el único cambio permitido. Los directorios físicos `ante/`/`ante_nf/` se conservan
hasta la autorización de cutover; aquí solo se retira su AUTORIDAD en el código, de forma medible.

    python -m tools.check_no_legacy_envs      # exit 1 si aparece uso legacy nuevo o el baseline quedó stale
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

_SUBPROC_ATTRS = {"run", "Popen", "check_output", "check_call", "call"}
_OS_ATTRS = {"system", "popen"}


def _py_legacy_count(text: str) -> int:
    """Cuenta usos EJECUTABLES de ante/ante_nf en Python (subprocess/os.system/os.popen con una ruta
    `ante*/bin/` en argv), vía AST — NO comentarios, docstrings ni menciones sueltas."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    subp = {"subprocess"}
    osm = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    subp.add(a.asname or a.name)
                elif a.name == "os":
                    osm.add(a.asname or a.name)
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        is_sub = (
            isinstance(f, ast.Attribute)
            and f.attr in _SUBPROC_ATTRS
            and isinstance(f.value, ast.Name)
            and f.value.id in subp
        ) or (isinstance(f, ast.Name) and f.id == "Popen")
        is_os = (
            isinstance(f, ast.Attribute) and f.attr in _OS_ATTRS and isinstance(f.value, ast.Name) and f.value.id in osm
        )
        if not (is_sub or is_os):
            continue
        strs = []
        a0 = node.args[0]
        if isinstance(a0, (ast.List, ast.Tuple)):
            strs = [e.value for e in a0.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        elif isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            strs = [a0.value]
        if any(_LEGACY.search(s) for s in strs):
            n += 1
    return n


ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "legacy_env_baseline.json"
_LEGACY = re.compile(r"\bante(_nf)?/bin/")
# B16: incluye .py (una `subprocess.run(["ante_nf/bin/python", …])` versionada también cuenta).
_SCAN_EXT = (".sh", ".yml", ".yaml", ".py")
_SCAN_BASE = ("Makefile", "dvc.yaml")


def _tracked(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(root), capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(f"check_no_legacy_envs: `git ls-files` falló ({exc}) — fail-closed") from exc
    return [f for f in out.split("\0") if f and (f.endswith(_SCAN_EXT) or Path(f).name in _SCAN_BASE)]


def current_counts(root: Path = ROOT) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in _tracked(root):
        try:
            text = (root / rel).read_text()
        except OSError, UnicodeDecodeError:
            continue
        # .py: solo usos EJECUTABLES (AST); shell/yaml/make: refs de línea de comando (regex).
        n = _py_legacy_count(text) if rel.endswith(".py") else len(_LEGACY.findall(text))
        if n:
            counts[rel] = n
    return counts


def check(root: Path = ROOT) -> list[str]:
    doc = json.loads(BASELINE.read_text())
    baseline = doc.get("max_per_file", {})
    counts = current_counts(root)
    probs: list[str] = []
    # B16: el campo `total` DEBE ser exactamente la suma del baseline (sin holgura oculta).
    if doc.get("total") != sum(baseline.values()):
        probs.append(f"baseline.total={doc.get('total')} != sum(max_per_file)={sum(baseline.values())}")
    for rel, n in counts.items():
        allowed = baseline.get(rel, 0)
        if n > allowed:
            probs.append(f"{rel}: {n} refs a ante/ante_nf (baseline {allowed}) — migra a `python_env run-python`")
    # baseline stale: un fichero que YA migró (bajó a 0) debe salir del baseline
    for rel, allowed in baseline.items():
        if allowed and counts.get(rel, 0) < allowed:
            probs.append(
                f"{rel}: bajó de {allowed} a {counts.get(rel, 0)} refs — ACTUALIZA docs/legacy_env_baseline.json (el trinquete no permite holgura)"
            )
    return probs


def main() -> int:
    probs = check()
    if probs:
        print("✗ CHECK NO-LEGACY-ENVS (trinquete C4/B2):")
        for p in probs:
            print(f"  - {p}")
        return 1
    total = sum(current_counts().values())
    print(
        f"✓ Sin uso legacy nuevo: {total} refs a ante/ante_nf en el call graph (trinquete; migración en curso a run-python)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
