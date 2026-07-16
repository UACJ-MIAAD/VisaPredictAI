#!/usr/bin/env python
"""Gate AST (P0R.5 · B148/B145 · Incremento 1R3 · B179/B180): prohíbe primitivas destructivas CRUDAS del sistema de
archivos en la capa de bundle. `tools/campaign_bundle.py` NO puede llamar `os.unlink`/`os.remove`/`os.rename`/
`os.replace`/`os.rmdir` directamente — toda mutación destructiva debe pasar por la cuarentena gobernada de
`tools/governed_fs.py` (fd-bound, verificada), que es la ÚNICA implementación autorizada. Así se elimina de raíz el
patrón check→unlink (ventana TOCTOU) en la capa de autoridad.

Uso: `python -m tools.check_raw_fs_mutations` (salida 0/1). Se ejecuta en CI y en el gate local."""

from __future__ import annotations

import ast
import os
import sys

_FORBIDDEN = frozenset({"unlink", "remove", "rename", "replace", "rmdir"})
# Ficheros de la capa de bundle que NO pueden mutar destructivamente por su cuenta (usan governed_fs).
_GUARDED = ("tools/campaign_bundle.py",)
# La ÚNICA implementación autorizada de primitivas destructivas gobernadas.
_ALLOWED = ("tools/governed_fs.py",)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _violations(path: str) -> list[str]:
    with open(path, "rb") as fh:
        tree = ast.parse(fh.read(), filename=path)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _FORBIDDEN:
            continue
        val = node.func.value
        if isinstance(val, ast.Name) and val.id == "os":  # os.unlink(...) etc.
            out.append(f"{path}:{node.lineno}: os.{node.func.attr}(...) crudo en capa de bundle (usa governed_fs)")
    return out


def main() -> int:
    problems: list[str] = []
    for rel in _GUARDED:
        problems.extend(_violations(os.path.join(_ROOT, rel)))
    # sanidad: governed_fs SÍ debe concentrar las primitivas (si no, la separación es ilusoria)
    concentrated = sum(1 for rel in _ALLOWED for _ in _violations_relaxed(os.path.join(_ROOT, rel)))
    if concentrated == 0:
        problems.append("governed_fs.py no contiene primitivas destructivas: la concentración es ilusoria")
    if problems:
        for p in problems:
            print(f"✗ {p}", file=sys.stderr)
        return 1
    print(f"✓ 0 mutaciones destructivas crudas en {', '.join(_GUARDED)}; concentradas en {', '.join(_ALLOWED)}")
    return 0


def _violations_relaxed(path: str) -> list[str]:
    """Como `_violations` pero cuenta también `os.unlink(x, dir_fd=fd)` (positional o kw) en el módulo autorizado."""
    return _violations(path)


if __name__ == "__main__":
    raise SystemExit(main())
