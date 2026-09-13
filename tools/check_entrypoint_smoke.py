"""Smoke de entrypoints: ¿resuelve lo que cada guion importa, y arranca en SU intérprete?

Motivación medida, no hipotética. La campaña `rederiv_1022c9d_20260911T212150` murió a las **10
horas** porque `save_finalists_deep.py` hacía ``from run_global_deep import _auto_config`` y ese
símbolo no existía desde `0a9ebcc`. Coste: una jornada de cómputo para descubrir un `ImportError`.

**Dos comprobaciones, porque una sola no basta:**

1. **Símbolos cruzados (AST, sin ejecutar).** El import roto vivía **dentro de `main()`**, no a
   nivel de módulo: importar el archivo NO lo habría detectado. Aquí se resuelve cada
   ``from <hermano> import a, b`` —esté donde esté, incluso anidado en una función— contra los
   nombres que ese hermano define de verdad. No necesita las dependencias pesadas: es lectura.
2. **Arranque real en su intérprete (subproceso limpio).** Lo que el AST no ve: dependencias
   ausentes, errores de sintaxis en el árbol de imports, un paquete que cambió de nombre. Cada
   guion se importa en `ante` o `ante_nf` según **quién lo invoca en los runners**, no según una
   lista escrita a mano.

⚠️ **Los subprocesos arrancan limpios a propósito.** Heredar el entorno del proceso padre —sus
``sys.modules`` parcheados, su ``PYTHONPATH``, sus monkeypatches de pytest— es exactamente la
falsa cobertura de M72: la prueba pasaba en el padre y el job real fallaba. Aquí se pasa un
entorno mínimo y explícito, y el intérprete se elige por ruta absoluta.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS = ROOT / "experiments"
#: Quién invoca a quién, derivado de los runners. `$NF`/`ante_nf` ⇒ perfil deep.
NF_CALL = re.compile(r"(?:\$NF|ante_nf/bin/python)\s+(experiments/[a-z0-9_]+\.py)")
ANTE_CALL = re.compile(r"(?:\$ANTE|ante/bin/python)\s+(experiments/[a-z0-9_]+\.py)")
RUNNERS = ("experiments/*.sh", "Makefile")
INTERPRETERS = {"ante": ROOT / "ante" / "bin" / "python", "ante_nf": ROOT / "ante_nf" / "bin" / "python"}


class SmokeError(RuntimeError):
    """Un entrypoint no resuelve sus símbolos o no arranca en su intérprete."""


# ─────────────────────────────────────────────────────── 1 · símbolos cruzados (AST)
def _top_level_names(modulo: Path) -> set[str]:
    """Nombres que un módulo define en su nivel superior: def, class, asignaciones e imports."""
    nombres: set[str] = set()
    for nodo in ast.parse(modulo.read_text(encoding="utf-8")).body:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.add(nodo.name)
        elif isinstance(nodo, ast.Assign):
            nombres.update(t.id for t in nodo.targets if isinstance(t, ast.Name))
        elif isinstance(nodo, ast.AnnAssign) and isinstance(nodo.target, ast.Name):
            nombres.add(nodo.target.id)
        elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
            nombres.update(a.asname or a.name.split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.Try):  # imports con fallback
            for rama in (*nodo.body, *nodo.handlers, *nodo.orelse):
                cuerpo = rama.body if isinstance(rama, ast.ExceptHandler) else [rama]
                for hijo in cuerpo:
                    if isinstance(hijo, (ast.Import, ast.ImportFrom)):
                        nombres.update(a.asname or a.name.split(".")[0] for a in hijo.names)
    return nombres


def cross_module_symbols(directorio: Path = EXPERIMENTS) -> list[str]:
    """Todo ``from <hermano> import nombre`` que el hermano NO define. Incluye los anidados."""
    hermanos = {p.stem: p for p in sorted(directorio.glob("*.py"))}
    cache: dict[str, set[str]] = {}
    problemas: list[str] = []
    for stem, ruta in hermanos.items():
        for nodo in ast.walk(ast.parse(ruta.read_text(encoding="utf-8"))):
            if not isinstance(nodo, ast.ImportFrom) or nodo.level or nodo.module not in hermanos:
                continue
            objetivo = nodo.module
            if objetivo not in cache:
                cache[objetivo] = _top_level_names(hermanos[objetivo])
            for alias in nodo.names:
                if alias.name != "*" and alias.name not in cache[objetivo]:
                    problemas.append(
                        f"{directorio.name}/{stem}.py:{nodo.lineno}: importa {alias.name!r} de "
                        f"{objetivo!r}, que no lo define"
                    )
    return problemas


# ─────────────────────────────────────────────────────── 2 · arranque real, subproceso limpio
def interpreter_of(script: str, root: Path = ROOT) -> str:
    """El perfil con el que los RUNNERS invocan ese guion. Derivado, no tecleado."""
    for patron in RUNNERS:
        for runner in sorted(root.glob(patron)):
            texto = runner.read_text(encoding="utf-8", errors="ignore")
            if script in NF_CALL.findall(texto):
                return "ante_nf"
            if script in ANTE_CALL.findall(texto):
                return "ante"
    return "ante"


def _clean_env(root: Path) -> dict[str, str]:
    """Entorno MÍNIMO y explícito: nada de heredar parches del padre (lección de M72)."""
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.environ.get("HOME", str(root)),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(root / "experiments") + os.pathsep + str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        # §8: el acelerador oficial. Sin él, el smoke no reproduce la corrida gobernada.
        "VP_DEEP_ACCEL": "cpu",
    }


def import_in_subprocess(script: str, perfil: str, root: Path = ROOT, timeout: int = 300) -> tuple[bool, str]:
    """Importa el guion en un proceso NUEVO del intérprete que le toca."""
    interp = INTERPRETERS[perfil] if perfil in INTERPRETERS else root / perfil / "bin" / "python"
    if not interp.is_file():
        return False, f"no existe el intérprete {interp}"
    modulo = Path(script).stem
    try:
        fin = subprocess.run(
            [str(interp), "-c", f"import {modulo}"],
            cwd=str(root),
            env=_clean_env(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if fin.returncode != 0:
        cola = (fin.stderr or fin.stdout or "").strip().splitlines()
        return False, " / ".join(cola[-3:])[:300]
    return True, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols-only", action="store_true", help="sólo el AST (no necesita venvs)")
    ap.add_argument("--scripts", nargs="*", default=None, help="acota a estos guiones")
    args = ap.parse_args(argv)

    fallos = cross_module_symbols()
    for f in fallos:
        print(f"✗ SÍMBOLO AUSENTE · {f}")

    if not args.symbols_only:
        guiones = args.scripts or sorted(f"experiments/{p.name}" for p in EXPERIMENTS.glob("*.py"))
        for script in guiones:
            perfil = interpreter_of(script)
            ok, motivo = import_in_subprocess(script, perfil)
            if ok:
                print(f"  ✓ {script} ({perfil})")
            else:
                fallos.append(f"{script} ({perfil}): {motivo}")
                print(f"✗ NO ARRANCA · {script} ({perfil}): {motivo}")

    if fallos:
        print(f"\n✗ SMOKE DE ENTRYPOINTS ROTO ({len(fallos)}): se detecta en segundos lo que la campaña")
        print("  tardaría horas en descubrir. Corrige antes de lanzar nada.")
        return 1
    alcance = "símbolos cruzados resueltos" + ("" if args.symbols_only else " y todos arrancan en su perfil")
    print(f"✓ smoke de entrypoints OK: {alcance}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
