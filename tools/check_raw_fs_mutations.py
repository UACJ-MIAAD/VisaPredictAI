#!/usr/bin/env python
"""Gate AST FAIL-CLOSED (P0R.5 · B148/B145 · Incremento 1R4 · B201) contra primitivas destructivas/de ejecución en
la ruta ONLINE del bundle. `tools/campaign_bundle.py` y `tools/governed_fs.py` (cuarentena MOVE-ONLY) NO pueden
llamar NINGUNA operación que borre/renombre/ejecute — el commit online sólo MUEVE y PRESERVA; el borrado físico es un
GC posterior separado (aún no implementado). Cierra los bypasses del gate ingenuo (B201): `import os as x`,
`from os import unlink`, alias propagados, `getattr(os, "unlink")`, `Path.unlink/rmdir/rename/replace`,
`shutil.rmtree/move`, `subprocess`/`os.system`/`os.exec*`/`os.spawn*`.

Uso: `python -m tools.check_raw_fs_mutations` (0/1). Escanea SÓLO ficheros trackeados por git; falla si git o el
parser fallan (fail-closed)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Ficheros de la ruta online del bundle: NINGUNA primitiva destructiva/de ejecución permitida.
_GUARDED = ("tools/campaign_bundle.py", "tools/governed_fs.py")

_OS_FORBIDDEN = frozenset({"unlink", "remove", "removedirs", "rename", "renames", "replace", "rmdir", "system", "truncate"})  # fmt: skip
_OS_FORBIDDEN_PREFIX = ("exec", "spawn", "popen")  # os.exec*, os.spawn*, os.popen
_PATH_FORBIDDEN = frozenset({"unlink", "rmdir", "rename", "replace", "hardlink_to", "symlink_to"})
_SHUTIL_FORBIDDEN = frozenset({"rmtree", "move", "copytree", "copy", "copy2"})
_NAME_FORBIDDEN = frozenset(_OS_FORBIDDEN | _PATH_FORBIDDEN | _SHUTIL_FORBIDDEN | {"rmtree"})


def _git_tracked(rel: str) -> bool:
    r = subprocess.run(["git", "-C", _ROOT, "ls-files", "--error-unmatch", rel], capture_output=True)
    return r.returncode == 0


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.problems: list[str] = []
        self.os_aliases: set[str] = {"os"}  # nombres que refieren al módulo os
        self.shutil_aliases: set[str] = {"shutil"}
        self.from_os: set[str] = set()  # nombres importados con `from os import X`
        self.from_shutil: set[str] = set()

    def _flag(self, node: ast.AST, what: str) -> None:
        self.problems.append(f"{self.path}:{getattr(node, 'lineno', 0)}: {what}")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name == "os":
                self.os_aliases.add(a.asname or "os")
            elif a.name == "shutil":
                self.shutil_aliases.add(a.asname or "shutil")
            elif a.name in ("pathlib", "subprocess"):
                self._flag(node, f"import {a.name} en la ruta online del bundle (potencial mutación/ejecución)")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os" and any(a.name in _OS_FORBIDDEN or a.name.startswith(_OS_FORBIDDEN_PREFIX) for a in node.names):  # fmt: skip
            self._flag(node, "from os import <primitiva destructiva/ejecución>")
        if node.module == "shutil" and any(a.name in _SHUTIL_FORBIDDEN for a in node.names):
            self._flag(node, "from shutil import <primitiva destructiva>")
        if node.module in ("pathlib", "subprocess"):
            self._flag(node, f"from {node.module} import … en la ruta online del bundle")
        for a in node.names:
            if node.module == "os":
                self.from_os.add(a.asname or a.name)
            elif node.module == "shutil":
                self.from_shutil.add(a.asname or a.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute):
            base = f.value
            attr = f.attr
            if isinstance(base, ast.Name):
                if base.id in self.os_aliases and (attr in _OS_FORBIDDEN or attr.startswith(_OS_FORBIDDEN_PREFIX)):
                    self._flag(node, f"os.{attr}(...) destructivo/ejecución (alias {base.id})")
                if base.id in self.shutil_aliases and attr in _SHUTIL_FORBIDDEN:
                    self._flag(node, f"shutil.{attr}(...) destructivo")
            if attr in _PATH_FORBIDDEN:  # Path(...).unlink() / p.rmdir() — cualquier receptor
                self._flag(node, f".{attr}(...) de Path (destructivo)")
            if attr == "getattr":  # os.getattr no aplica; getattr manejado abajo
                pass
        elif isinstance(f, ast.Name):
            if f.id == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                nm = node.args[1].value
                if isinstance(node.args[0], ast.Name) and node.args[0].id in self.os_aliases and nm in _OS_FORBIDDEN:
                    self._flag(node, f"getattr(os, {nm!r}) elude el gate")
            if f.id in self.from_os or f.id in self.from_shutil:  # nombre importado de os/shutil (destructivo)
                self._flag(node, f"{f.id}(...) importado de os/shutil (destructivo)")
        self.generic_visit(node)


def _scan(rel: str) -> list[str]:
    if not _git_tracked(rel):
        raise SystemExit(f"gate fail-closed: {rel} no está trackeado por git")
    with open(os.path.join(_ROOT, rel), "rb") as fh:
        tree = ast.parse(fh.read(), filename=rel)
    sc = _Scanner(rel)
    sc.visit(tree)
    return sc.problems


def main() -> int:
    try:
        problems = [p for rel in _GUARDED for p in _scan(rel)]
    except (OSError, SyntaxError, SystemExit) as exc:
        print(f"✗ gate AST fail-closed: {exc}", file=sys.stderr)
        return 1
    if problems:
        for p in problems:
            print(f"✗ {p}", file=sys.stderr)
        return 1
    print(f"✓ 0 primitivas destructivas/de ejecución en la ruta online del bundle: {', '.join(_GUARDED)}")
    return 0


# Compatibilidad con el test existente que llama `_violations(path)` sobre un fichero suelto.
def _violations(path: str) -> list[str]:
    with open(path, "rb") as fh:
        tree = ast.parse(fh.read(), filename=path)
    sc = _Scanner(path)
    sc.visit(tree)
    return sc.problems


if __name__ == "__main__":
    raise SystemExit(main())
