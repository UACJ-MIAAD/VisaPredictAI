#!/usr/bin/env python
"""Gate FAIL-CLOSED de APERTURAS SEGURAS en la ruta online del bundle (P0R.5 · B218/B219).

Complementa a `check_raw_fs_mutations.py` (que prohíbe primitivas destructivas). Aquí se exige que TODA apertura
`os.open(...)` en los cuatro ficheros de la ruta online no pueda COLGAR sobre un objeto especial (FIFO/socket/
dispositivo) que un tercero del mismo UID haya sustituido por NOMBRE:

- Apertura de DIRECTORIO (contiene `O_DIRECTORY`)  → exige `O_NOFOLLOW` garantizado (un FIFO da ENOTDIR, no cuelga).
- Apertura de CREACIÓN (contiene `O_CREAT`)        → exige `O_EXCL` + `O_NOFOLLOW` garantizados (O_EXCL rechaza un
                                                     nombre preexistente, sea cual sea su tipo → no cuelga).
- Cualquier otra apertura (lectura/RW sobre un nombre EXISTENTE y por tanto sustituible) → exige `O_NOFOLLOW` **y**
  `O_NONBLOCK` garantizados (un FIFO/dispositivo NO cuelga el open; el tipo se valida por `fstat` antes de leer).
- Flags NO resolubles estáticamente a `os.O_*` → FALLO (fail-closed).
- `O_TRUNC` en cualquier rama posible → FALLO (mutación destructiva; redundante con el otro gate).

Las lecturas de CONTENIDO gobernadas se canalizan por `governed_read.opened_regular_noblock_at` (que exige
`S_ISREG` por `fstat` ANTES de leer) — este gate garantiza que NO exista una apertura de lectura por fuera de esa
invariante. Escanea SÓLO ficheros versionados (`git ls-files`); si git falla o un fichero no parsea, FALLA cerrado.
"""

from __future__ import annotations

import ast
import subprocess
import sys

_GUARDED = (
    "tools/governed_read.py",
    "tools/governed_fs.py",
    "tools/campaign_bundle.py",
    "tools/merge_campaign_pools.py",
)


def _git_tracked(rel: str) -> bool:
    try:
        out = subprocess.run(["git", "ls-files", "--error-unmatch", rel], capture_output=True, text=True)
    except OSError:
        return False
    return out.returncode == 0


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.problems: list[str] = []
        self.os_aliases: set[str] = {"os"}
        self.from_os: set[str] = set()
        self.open_aliases: set[str] = set()  # nombres que refieren a os.open (`myopen = os.open`)
        self.flag_consts: dict[str, tuple[set[str], set[str]]] = {}

    def prescan(self, tree: ast.AST) -> None:
        for _ in range(4):  # varias pasadas para resolver constantes de flags encadenadas (_DIR_FLAGS = os.O_* | …)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    bounds = self._flag_bounds(node.value)
                    if bounds is not None:
                        self.flag_consts[node.targets[0].id] = bounds
                    v = node.value  # alias de os.open: `f = os.open` — se chequea como os.open (fail-closed)
                    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id in self.os_aliases and v.attr == "open":  # fmt: skip
                        self.open_aliases.add(node.targets[0].id)

    def _flag(self, node: ast.AST, what: str) -> None:
        self.problems.append(f"{self.path}:{getattr(node, 'lineno', 0)}: {what}")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name == "os":
                self.os_aliases.add(a.asname or "os")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for a in node.names:
                self.from_os.add(a.asname or a.name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "__dict__" and isinstance(node.value, ast.Name) and node.value.id in self.os_aliases:
            self._flag(node, "os.__dict__ (elude el gate de aperturas)")  # os.__dict__['open'] esquiva el análisis
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.os_aliases and f.attr == "open":  # fmt: skip
            self._check_os_open(node)
        elif isinstance(f, ast.Name) and f.id in self.open_aliases:  # alias `myopen = os.open` → mismas reglas
            self._check_os_open(node)
        elif isinstance(f, ast.Name):
            if f.id == "getattr" and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in self.os_aliases:  # fmt: skip
                self._flag(node, "getattr(os, …) dinámico (elude el gate de aperturas)")
            if f.id in ("__import__", "exec", "eval", "compile"):
                self._flag(node, f"{f.id}(...) prohibido en la ruta online (puede eludir el gate)")
        self.generic_visit(node)

    def _check_os_open(self, node: ast.Call) -> None:
        bounds = self._flag_bounds(node.args[1]) if len(node.args) >= 2 else None
        if bounds is None:
            self._flag(node, "os.open con flags DINÁMICOS (no resolubles a os.O_*) — fail-closed")
            return
        guaranteed, possible = bounds
        if "O_TRUNC" in possible:
            self._flag(node, "os.open con O_TRUNC (mutación destructiva)")
        if "O_NOFOLLOW" not in guaranteed:
            self._flag(node, "os.open sin O_NOFOLLOW garantizado")
            return
        if "O_DIRECTORY" in guaranteed:
            return  # apertura de dir: O_NOFOLLOW basta (un FIFO da ENOTDIR)
        if "O_CREAT" in guaranteed:
            if "O_EXCL" not in guaranteed:  # crear sin O_EXCL podría abrir un FIFO/dispositivo preexistente
                self._flag(node, "os.open con O_CREAT sin O_EXCL garantizado (podría abrir un objeto preexistente)")
            return
        if "O_NONBLOCK" not in guaranteed:  # lectura/RW sobre nombre existente y sustituible → debe ser no bloqueante
            self._flag(node, "os.open de lectura sobre nombre sustituible sin O_NONBLOCK garantizado (podría colgar en un FIFO)")  # fmt: skip

    def _flag_bounds(self, expr: ast.AST) -> tuple[set[str], set[str]] | None:
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
        if isinstance(expr, ast.Name) and expr.id in self.flag_consts:
            return self.flag_consts[expr.id]
        return None


def _scan(rel: str) -> list[str]:
    if not _git_tracked(rel):
        return [f"{rel}: NO versionado o git ls-files falló (fail-closed)"]
    try:
        with open(rel, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
    except (OSError, SyntaxError) as exc:
        return [f"{rel}: no parseable ({exc}) (fail-closed)"]
    sc = _Scanner(rel)
    sc.prescan(tree)
    sc.visit(tree)
    return sc.problems


def _violations(path: str) -> list[str]:
    """Escanea un fichero ARBITRARIO (para tests) sin exigir que esté versionado."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    sc = _Scanner(path)
    sc.prescan(tree)
    sc.visit(tree)
    return sc.problems


def main() -> int:
    problems = [p for rel in _GUARDED for p in _scan(rel)]
    if problems:
        print("✗ aperturas inseguras en la ruta online del bundle:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ 0 aperturas que puedan colgar sobre un objeto especial: {', '.join(_GUARDED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
