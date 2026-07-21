#!/usr/bin/env python
"""Gate FAIL-CLOSED de APERTURAS SEGURAS con REGISTRO POSITIVO (P0R.5 · B218/B219/B220 · 1R7R2).

Complementa a `check_raw_fs_mutations.py` (primitivas destructivas). Aquí se garantiza que en la ruta online del
bundle NINGUNA lectura pueda COLGAR sobre un objeto especial (FIFO/socket/dispositivo) sustituido por nombre, y que
TODA lectura pase por la FUENTE ÚNICA de apertura segura. Reglas (todas fail-closed):

1. La ÚNICA función autorizada a hacer `os.open` de LECTURA (O_RDONLY, sin O_CREAT/O_DIRECTORY) es
   `governed_read.opened_regular_noblock_at` (registro positivo). Cualquier otra `os.open` de lectura → FALLO.
2. Toda `os.open` de lectura autorizada exige `O_NOFOLLOW | O_NONBLOCK`; de dir exige `O_DIRECTORY | O_NOFOLLOW`;
   de creación exige `O_CREAT | O_EXCL | O_NOFOLLOW`. `O_TRUNC` o flags no resolubles a `os.O_*` → FALLO.
3. `open(...)` de builtins, `io.open(...)`, `Path(...).read_text/read_bytes`, `<x>.read_text/read_bytes` en un
   módulo online → FALLO (deben pasar por los helpers gobernados `read_bytes_at`/`read_bytes_path`/`read_bytes_abs`).
4. Indirecciones que eluden el análisis → FALLO: alias `f = os.open`, `getattr(os, …)`, `os.__dict__`,
   `from os import open`, `__import__`/`exec`/`eval`/`compile`.
5. INVENTARIO: si un `tools/*.py` (no test, no este gate) importa la maquinaria online (`GovernedQuarantine`,
   `import tools.campaign_bundle`, `import tools.merge_campaign_pools`) sin estar en el inventario → FALLO.

Escanea SÓLO ficheros versionados (`git ls-files`); si git falla o un fichero no parsea, FALLA cerrado.
"""

from __future__ import annotations

import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)  # B286-B: raíz del repo en sys.path para importar `tools.governance_snapshot` como script
_ONLINE = (
    "tools/governed_read.py",
    "tools/governed_fs.py",
    "tools/campaign_bundle.py",
    "tools/merge_campaign_pools.py",
)
# Registro POSITIVO: (fichero, función) donde una os.open de LECTURA (O_RDONLY) está autorizada — la fuente única.
_READ_OPEN_ALLOWED: frozenset[tuple[str, str]] = frozenset({("tools/governed_read.py", "opened_regular_noblock_at")})
# Registro POSITIVO de aperturas WR/RW (locks/manifiestos): (fichero, función) — NUNCA una lectura genérica.
_RDWR_OPEN_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("tools/merge_campaign_pools.py", "_acquire_lock"),
        ("tools/campaign_bundle.py", "_authority_lock"),
    }  # B240: lock gobernado
)
_READ_LIKE_ATTRS = frozenset({"read_text", "read_bytes"})  # Path(...).read_text() y variantes


def _analyze_source(rel: str, data: bytes) -> list[str]:
    """Núcleo PURO del scanner de aperturas: parsea `data` (bytes gobernados) y devuelve problemas. Sin I/O."""
    tree = ast.parse(data, filename=rel)
    sc = _Scanner(rel)
    sc.prescan(tree)
    sc.visit(tree)
    return sc.problems


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.problems: list[str] = []
        self.os_aliases: set[str] = {"os"}
        self.io_aliases: set[str] = {"io"}
        self.from_os: set[str] = set()
        self.open_aliases: set[str] = set()  # nombres que refieren a os.open o al builtin open
        self.flag_consts: dict[str, tuple[set[str], set[str]]] = {}
        self._func_stack: list[str] = []

    def prescan(self, tree: ast.AST) -> None:
        for node in ast.walk(
            tree
        ):  # PRIMERO los alias de módulo (os/io), para resolver `f = o.open` con `import os as o`
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "os":
                        self.os_aliases.add(a.asname or "os")
                    elif a.name == "io":
                        self.io_aliases.add(a.asname or "io")
        for _ in range(4):  # varias pasadas para resolver constantes de flags encadenadas (_DIR_FLAGS = os.O_* | …)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    bounds = self._flag_bounds(node.value)
                    if bounds is not None:
                        self.flag_consts[node.targets[0].id] = bounds
                    v = node.value  # alias de os.open o del builtin open
                    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id in self.os_aliases and v.attr == "open":  # fmt: skip
                        self.open_aliases.add(node.targets[0].id)
                    if isinstance(v, ast.Name) and v.id == "open":
                        self.open_aliases.add(node.targets[0].id)

    def _flag(self, node: ast.AST, what: str) -> None:
        self.problems.append(f"{self.path}:{getattr(node, 'lineno', 0)}: {what}")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name == "os":
                self.os_aliases.add(a.asname or "os")
            elif a.name == "io":
                self.io_aliases.add(a.asname or "io")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for a in node.names:
                self.from_os.add(a.asname or a.name)
                if a.name == "open":
                    self._flag(node, "from os import open (elude el gate de aperturas)")
        if node.module == "io":
            self._flag(node, "from io import … en la ruta online (apertura no gobernada)")
        self.generic_visit(node)

    def _current_func(self) -> str:
        return self._func_stack[-1] if self._func_stack else "<módulo>"

    def _enter_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_func(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "__dict__" and isinstance(node.value, ast.Name) and node.value.id in self.os_aliases:
            self._flag(node, "os.__dict__ (elude el gate de aperturas)")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id in self.os_aliases and f.attr == "open":
                self._check_os_open(node)
            elif isinstance(f.value, ast.Name) and f.value.id in self.io_aliases and f.attr == "open":
                self._flag(node, "io.open(...) en la ruta online (apertura no gobernada)")
            elif f.attr in _READ_LIKE_ATTRS:  # Path(...).read_text() / p.read_bytes() — lectura por ruta sin gobernar
                self._flag(node, f".{f.attr}() (lectura por ruta sin apertura gobernada no bloqueante)")
        elif isinstance(f, ast.Name):
            if f.id == "open" or f.id in self.open_aliases:  # builtin open o alias de os.open/open
                self._flag(node, f"{f.id}(...) (apertura no gobernada; usar read_bytes_* / opened_regular_noblock_*)")
            elif f.id == "getattr" and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in self.os_aliases:  # fmt: skip
                self._flag(node, "getattr(os, …) dinámico (elude el gate de aperturas)")
            elif f.id in ("__import__", "exec", "eval", "compile"):
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
            if "O_EXCL" not in guaranteed:
                self._flag(node, "os.open con O_CREAT sin O_EXCL garantizado (podría abrir un objeto preexistente)")
            return
        if (
            "O_WRONLY" in guaranteed or "O_RDWR" in guaranteed
        ):  # apertura WR/RW (lock/manifiesto): SÓLO sitios registrados
            if (self.path, self._current_func()) not in _RDWR_OPEN_ALLOWED:
                self._flag(node, f"os.open WR/RW fuera del registro de locks (en {self._current_func()}) — nunca como lectura genérica")  # fmt: skip
                return
            if "O_NONBLOCK" not in guaranteed:
                self._flag(node, "os.open WR/RW sin O_NONBLOCK garantizado (podría colgar en un FIFO)")
            return
        # LECTURA O_RDONLY de contenido: SÓLO la fuente única puede hacerla, y debe ser no bloqueante
        if (self.path, self._current_func()) not in _READ_OPEN_ALLOWED:
            self._flag(node, f"os.open de LECTURA fuera de la fuente única (en {self._current_func()}) — usar opened_regular_noblock_*")  # fmt: skip
            return
        if "O_NONBLOCK" not in guaranteed:
            self._flag(node, "os.open de lectura sin O_NONBLOCK garantizado (podría colgar en un FIFO)")

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


def _violations(path: str) -> list[str]:
    """Escanea un fichero ARBITRARIO (para tests, fixtures fuera del repo) sin exigir que esté versionado."""
    with open(path, encoding="utf-8") as fh:
        return _analyze_source(path, fh.read().encode("utf-8"))


def _online_import_problems(rel: str, tree: ast.AST) -> list[str]:
    """Marcadores de maquinaria online en `tree`: importar `GovernedQuarantine` o la maquinaria del bundle en
    CUALQUIER forma (`import tools.campaign_bundle`, `from tools.campaign_bundle import …`, `from tools.governed_fs
    import GovernedQuarantine`)."""
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools.governed_fs" and any(a.name == "GovernedQuarantine" for a in node.names):  # fmt: skip
            problems.append(f"{rel}: importa GovernedQuarantine pero no está en el inventario online")
        if isinstance(node, ast.ImportFrom) and node.module in ("tools.campaign_bundle", "tools.merge_campaign_pools"):
            problems.append(f"{rel}: 'from {node.module} import …' — maquinaria online sin inventariar")
        if isinstance(node, ast.Import) and any(a.name in ("tools.campaign_bundle", "tools.merge_campaign_pools") for a in node.names):  # fmt: skip
            problems.append(f"{rel}: importa la maquinaria online (campaign_bundle/merge) sin inventariar")
    return problems


_SELF = "tools/check_safe_opens.py"


def main() -> int:
    # B286-B: una sola observación gobernada (`GovernanceSnapshot`): inventario Git SELLADO una vez + `snap.read()` (sin
    # `git ls-files` ni `open()` ad hoc), `reverify()` antes del éxito, cierre único. Cualquier error de lectura/inventario/
    # parseo/cierre es ROJO (fail-closed).
    from tools.governance_snapshot import GovernanceSnapshot, GovernanceSnapshotError, TrackedQuery

    try:
        with GovernanceSnapshot(_ROOT) as snap:
            tracked = set(snap.tracked(TrackedQuery("suffix", ".py")))  # inventario SELLADO, una sola captura Git
            problems: list[str] = []
            for rel in _ONLINE:  # ruta online: cada apertura debe pasar por la fuente única y no colgar
                if rel not in tracked:
                    problems.append(f"{rel}: NO versionado en el inventario sellado (fail-closed)")
                    continue
                problems.extend(_analyze_source(rel, snap.read(rel, category="source").data))
            # INVENTARIO: todo `tools/*.py` (no _ONLINE, no este gate) que importe la maquinaria online sin gobernar
            for rel in sorted(r for r in tracked if r.startswith("tools/") and r != _SELF and r not in _ONLINE):
                data = snap.read(rel, category="source").data
                problems.extend(_online_import_problems(rel, ast.parse(data, filename=rel)))
            snap.reverify()  # re-sella la observación tras la última lectura y antes del éxito
    except (GovernanceSnapshotError, OSError, SyntaxError) as exc:
        print(f"✗ gate fail-closed (aperturas seguras / inventario online): {exc}")
        return 1
    if problems:
        print("✗ aperturas inseguras / inventario online:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ 0 aperturas que puedan colgar; lecturas single-sourced; inventario íntegro: {', '.join(_ONLINE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
