#!/usr/bin/env python
"""Gate P0R.5 (R4): DVC se invoca EXCLUSIVAMENTE por la interfaz única

    python -m tools.python_env exec --profile dvc-tool -- dvc <args>

y se instala EXCLUSIVAMENTE por `tools/python_env.py` (del lock dvc-tool hasheado, en un entorno
content-addressed aislado). Cualquier otra ruta reintroduciría `diskcache 5.6.3` (PYSEC-2026-2447) o
degradaría las deps del producto (dvc[s3] fija requests/tqdm más nuevos).

Escanea TODOS los ficheros versionados relevantes (`git ls-files`: *.yml, *.yaml, *.sh, *.py, Makefile,
dvc.yaml, *.md) y BLOQUEA:
  - binario/instalación legacy: `ante/bin/dvc`, `DVC_BIN`, `dvc_cache_guard --run`, `pip install <dvc>`;
  - invocación shell de `dvc <verbo|--flag>` que NO pase por el wrapper (`… exec … -- dvc …`);
  - invocación Python de dvc: `subprocess.run/Popen/check_output/check_call([... "dvc" ...])`,
    `os.system("dvc …")`, `os.popen("dvc …")`;
  - uso de `$DVC`/`$(DVC)`/`${DVC}` en un fichero que NO defina `DVC` como el wrapper.

Sin excepciones generales de `$DVC`/`$(DVC)`. Único fichero autorizado a instalar/lanzar el
console-script dvc: `tools/python_env.py` (el wrapper).

    python -m tools.check_no_stray_dvc      # exit 1 ante cualquier ruta prohibida
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = "tools/python_env.py"  # único autorizado a construir/lanzar dvc
# Auto-exclusión justificada: el wrapper (invoca dvc por diseño) y ESTE gate (define los patrones
# prohibidos en su docstring/regex; un linter no puede lintar sus propias definiciones).
_ALLOWLIST = frozenset({WRAPPER, "tools/check_no_stray_dvc.py"})
_SCAN_EXT = (".yml", ".yaml", ".sh", ".py", ".md")
_SCAN_BASE = ("Makefile", "dvc.yaml")

# `pip install ... dvc ...` / `pipx install dvc` / `uv tool install dvc`; NO matchea `.[dev]`/`.[model]`.
_PIP_DVC = re.compile(
    r"(?:pip\s+install|pipx\s+install|uv\s+tool\s+install)\b.*(?<![\w./-])dvc(?:\[[^\]]*\])?(?:==|\b)"
)
# invocación shell de dvc en posición de comando: inicio, tras ;/&/|/&&/||, o tras `run:`/`--`.
_SH_DVC = re.compile(r"(?:^|[;&|]|&&|\|\||run:|--)\s*dvc(?:\.exe)?\s+\S")
# `python -m dvc …` (ejecutar dvc como módulo, evadiendo el wrapper).
_SH_PYM_DVC = re.compile(r"python[0-9.]*\s+-m\s+dvc\b")
# el wrapper: `… tools.python_env exec … -- dvc …`
_WRAPPER_CALL = re.compile(r"tools\.python_env\s+exec\b.*--\s+dvc\b")
# uso y definición de la variable DVC
_USE_DVC_VAR = re.compile(r"\$\{?\(?DVC\b")
_DEF_DVC_WRAPPER = re.compile(r"\bDVC\s*[:?]?=.*python_env\s+exec\b.*dvc\b")
_DVC_BIN = re.compile(r"\bDVC_BIN\b")
_LEGACY = (
    ("ante/bin/dvc", "referencia al binario dvc legacy (ante/bin/dvc)"),
    ("dvc_cache_guard --run", "interfaz `dvc_cache_guard --run` eliminada (usar python_env exec)"),
)


def _tracked_files(root: Path) -> list[Path]:
    # FAIL-CLOSED: si `git ls-files` falla, NO devolvemos [] (eso dejaría pasar todo) — reventamos.
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(root), capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(f"check_no_stray_dvc: `git ls-files` falló en {root} ({exc}) — fail-closed") from exc
    files = []
    for rel in out.split("\0"):
        if not rel:
            continue
        p = root / rel
        if p.suffix in _SCAN_EXT or p.name in _SCAN_BASE:
            files.append(p)
    return files


_SUBPROC_ATTRS = {"run", "Popen", "check_output", "check_call", "call"}
_OS_ATTRS = {"system", "popen"}


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _str_const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _import_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """(nombres ligados a subprocess, nombres ligados a os, funciones de subprocess importadas
    directamente). Cierra el evasion `import subprocess as sp` / `from subprocess import run as r`."""
    subp, osm, subp_funcs = {"subprocess"}, {"os"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    subp.add(a.asname or a.name)
                elif a.name == "os":
                    osm.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name in _SUBPROC_ATTRS:
                    subp_funcs.add(a.asname or a.name)
    return subp, osm, subp_funcs


def _seq_tokens(node: ast.AST) -> list[str | None] | None:
    """Tokens string de una List o Tuple argv, o None si no lo es."""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return [_str_const(e) for e in node.elts]
    return None


def _py_dvc_problems(text: str, rel: str) -> list[str]:
    """Invocaciones REALES de dvc en Python vía AST (NO cadenas de datos como `write_text('… dvc …')`).
    Cubre alias de import, argv en list/tuple, `python -m dvc`, y os.system/os.popen('dvc …')."""
    probs: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return probs
    subp_names, os_names, subp_funcs = _import_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_subproc = (
            isinstance(func, ast.Attribute)
            and func.attr in _SUBPROC_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id in subp_names
        ) or (isinstance(func, ast.Name) and func.id in subp_funcs | {"Popen"})
        is_ossys = (
            isinstance(func, ast.Attribute)
            and func.attr in _OS_ATTRS
            and isinstance(func.value, ast.Name)
            and func.value.id in os_names
        )
        if is_subproc and (toks := _seq_tokens(node.args[0])) is not None:
            strs = [t for t in toks if t]
            if toks and toks[0] and _canon(toks[0]) == "dvc":
                probs.append(f"{rel}:{node.lineno}: invoca dvc por subprocess fuera del wrapper")
            # `python -m dvc` dentro del argv (evade el argv0)
            for a, b in zip(strs, strs[1:], strict=False):
                if a == "-m" and _canon(b) == "dvc":
                    probs.append(f"{rel}:{node.lineno}: ejecuta `-m dvc` por subprocess fuera del wrapper")
            if (
                {"install"} <= set(strs)
                and {"pip", "pipx", "uv"} & set(strs)
                and any(_canon(t).startswith("dvc") for t in strs if t not in ("pip", "pipx", "uv", "install", "tool"))
            ):
                probs.append(f"{rel}:{node.lineno}: instala dvc por subprocess fuera de tools/python_env.py")
        elif is_subproc and (s := _str_const(node.args[0])) and s.strip().split()[:1] == ["dvc"]:
            probs.append(f"{rel}:{node.lineno}: invoca dvc por subprocess (shell) fuera del wrapper")
        elif is_ossys and (s := _str_const(node.args[0])) and s.strip().split()[:1] == ["dvc"]:
            probs.append(f"{rel}:{node.lineno}: invoca dvc por os.system fuera del wrapper")
    return probs


def _strip_comment(line: str, ext: str) -> str:
    """Para shell/yaml/make/md quita `#`…; para .py deja la línea (los `#` de .py son código raro,
    y las cadenas con 'dvc' NO matchean los detectores de subprocess/os.system)."""
    if ext == ".py":
        return line
    out, in_s, in_d = [], False, False
    for ch in line:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            break
        out.append(ch)
    return "".join(out)


def check(root: Path = ROOT) -> list[str]:
    probs: list[str] = []
    for path in _tracked_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in _ALLOWLIST:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError, OSError:
            continue
        if path.suffix == ".py":
            # En Python la invocación REAL de dvc se detecta por AST (Call nodes), no por texto: las
            # cadenas de datos (fixtures `write_text('… dvc …')`) son Constant, no llamadas.
            probs += _py_dvc_problems(text, rel)
            continue
        # shell / yaml / make / docs
        if _USE_DVC_VAR.search(text) and not _DEF_DVC_WRAPPER.search(text):
            probs.append(f"{rel}: usa $DVC/$(DVC) sin definir DVC como el wrapper python_env exec")
        for i, raw in enumerate(text.splitlines(), 1):
            line = _strip_comment(raw, path.suffix)
            for tok, why in _LEGACY:
                if tok in line:
                    probs.append(f"{rel}:{i}: {why} -> {line.strip()}")
            if _DVC_BIN.search(line):
                probs.append(
                    f"{rel}:{i}: DVC_BIN prohibido (dvc solo desde el env content-addressed) -> {line.strip()}"
                )
            if _PIP_DVC.search(line):
                probs.append(f"{rel}:{i}: instala dvc fuera de tools/python_env.py -> {' '.join(line.split())}")
            if _SH_DVC.search(line) and not _WRAPPER_CALL.search(line):
                probs.append(f"{rel}:{i}: invoca dvc sin el wrapper python_env exec -> {line.strip()}")
            if _SH_PYM_DVC.search(line):
                probs.append(f"{rel}:{i}: ejecuta `python -m dvc` (evade el wrapper) -> {line.strip()}")
    return probs


def main() -> int:
    probs = check()
    if probs:
        print("✗ CHECK NO-STRAY-DVC bloqueó (P0R.5 R4):")
        for p in probs:
            print(f"  - {p}")
        return 1
    n = len(_tracked_files(ROOT))
    print(f"✓ DVC gobernado: {n} ficheros escaneados; dvc solo vía `python_env exec`, instalado solo por {WRAPPER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
