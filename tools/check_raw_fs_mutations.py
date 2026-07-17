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
        self.flag_consts: dict[str, tuple[set[str], set[str]]] = {}  # `_X = os.O_A | …` → (garantizado, posible)

    def prescan(self, tree: ast.AST) -> None:
        # B214: recoge constantes/variables de flags (varias pasadas para resolver constantes encadenadas)
        for _ in range(4):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    bounds = self._flag_bounds(node.value)
                    if bounds is not None:
                        self.flag_consts[node.targets[0].id] = bounds

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

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # B206: cualquier ACCESO a `os.<destructivo>` (llamada, ASIGNACIÓN a alias `f = os.unlink`, argumento…) o a
        # `.<Path-destructivo>`/`shutil.<destructivo>` — no solo las llamadas.
        base = node.value
        attr = node.attr
        if isinstance(base, ast.Name):
            if base.id in self.os_aliases and (attr in _OS_FORBIDDEN or attr.startswith(_OS_FORBIDDEN_PREFIX)):
                self._flag(node, f"acceso a os.{attr} destructivo/ejecución (alias {base.id})")
            if base.id in self.shutil_aliases and attr in _SHUTIL_FORBIDDEN:
                self._flag(node, f"acceso a shutil.{attr} destructivo")
        # Path(...).unlink / p.rmdir — cualquier receptor; EXCEPTO `dataclasses.replace` (construye un frozen NUEVO,
        # no toca el filesystem).
        if attr in _PATH_FORBIDDEN and not (attr == "replace" and isinstance(base, ast.Name) and base.id == "dataclasses"):  # fmt: skip
            self._flag(node, f"acceso a .{attr} de Path (destructivo)")
        if attr == "__dict__":  # os.__dict__["unlink"] elude el análisis
            self._flag(node, "acceso a __dict__ (elude el gate)")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name):
            if f.id == "getattr" and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in self.os_aliases:  # fmt: skip
                self._flag(node, "getattr(os, …) dinámico (elude el gate)")  # B206: cualquier getattr(os, …)
            if f.id in ("__import__", "exec", "eval", "compile"):
                self._flag(node, f"{f.id}(...) prohibido en la ruta online del bundle")
            if f.id in self.from_os or f.id in self.from_shutil:  # nombre importado de os/shutil (destructivo)
                self._flag(node, f"{f.id}(...) importado de os/shutil (destructivo)")
            if f.id == "open":  # B214: builtins.open en modo escritura (w/a/x/+) es una mutación destructiva
                mode = node.args[1] if len(node.args) >= 2 else None
                if mode is None or not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
                    self._flag(node, "open(...) sin modo constante (potencial escritura)")
                elif any(c in mode.value for c in "wax+"):
                    self._flag(node, f"open(..., {mode.value!r}) en modo escritura destructiva")
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.os_aliases and f.attr == "open":  # fmt: skip
            self._check_os_open(node)  # B214: os.open debe ser estático, con O_NOFOLLOW y sin O_TRUNC
        self.generic_visit(node)

    def _check_os_open(self, node: ast.Call) -> None:
        bounds = self._flag_bounds(node.args[1]) if len(node.args) >= 2 else None
        if bounds is None:
            self._flag(node, "os.open con flags DINÁMICOS (no resolubles a os.O_*)")
            return
        guaranteed, possible = bounds
        if "O_TRUNC" in possible:  # O_TRUNC posible en ALGUNA rama → mutación destructiva
            self._flag(node, "os.open con O_TRUNC (mutación destructiva)")
        if "O_NOFOLLOW" not in guaranteed:  # O_NOFOLLOW debe estar GARANTIZADO en TODAS las ramas
            self._flag(node, "os.open sin O_NOFOLLOW garantizado")

    def _flag_bounds(self, expr: ast.AST) -> tuple[set[str], set[str]] | None:
        # (garantizado, posible): `os.O_A | os.O_B`, IfExp de dos ramas, o Name de una constante recogida. None si
        # aparece algo NO estático.
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            left = self._flag_bounds(expr.left)
            right = self._flag_bounds(expr.right)
            return None if left is None or right is None else (left[0] | right[0], left[1] | right[1])
        if isinstance(expr, ast.IfExp):
            body = self._flag_bounds(expr.body)
            orelse = self._flag_bounds(expr.orelse)
            return None if body is None or orelse is None else (body[0] & orelse[0], body[1] | orelse[1])
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id in self.os_aliases and expr.attr.startswith("O_"):  # fmt: skip
            return ({expr.attr}, {expr.attr})
        if isinstance(expr, ast.Name) and expr.id in self.from_os and expr.id.startswith("O_"):
            return ({expr.id}, {expr.id})
        if isinstance(expr, ast.Name) and expr.id in self.flag_consts:  # `_DIR_FLAGS`/`flags` = os.O_* | …
            return self.flag_consts[expr.id]
        return None


def _scan(rel: str) -> list[str]:
    if not _git_tracked(rel):
        raise SystemExit(f"gate fail-closed: {rel} no está trackeado por git")
    with open(os.path.join(_ROOT, rel), "rb") as fh:
        tree = ast.parse(fh.read(), filename=rel)
    sc = _Scanner(rel)
    sc.prescan(tree)
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
    sc.prescan(tree)
    sc.visit(tree)
    return sc.problems


if __name__ == "__main__":
    raise SystemExit(main())
