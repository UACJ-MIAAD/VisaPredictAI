"""Smoke de entrypoints: detecta en minutos lo que una campaña tarda horas en descubrir.

Tres comprobaciones: que existan los símbolos importados entre módulos de `experiments/` (AST); que
cada entrypoint importe en un proceso NUEVO de su intérprete con entorno mínimo; y que cada tercero
del cierre local de los entrypoints de la campaña —de nivel de módulo o diferido dentro de una
función— se IMPORTE de verdad en ese intérprete. Es puerta obligatoria del runbook (M74-E-R6).
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
#: ★ M74-E-R4 · invocaciones `-m paquete.modulo`. Sin esto la comprobación de diferidos NO cazaba
#: `optuna`, que es el caso que la motivó: el HPO entra por `$ANTE -m vp_model.run_tuning`, no por
#: `experiments/*.py`. Un gate cuyo alcance excluye el defecto que lo originó no sirve de nada.
NF_MODULE = re.compile(r"(?:\$NF|ante_nf/bin/python)\s+-m\s+([a-z0-9_]+(?:\.[a-z0-9_]+)+)")
ANTE_MODULE = re.compile(r"(?:\$ANTE|ante/bin/python)\s+-m\s+([a-z0-9_]+(?:\.[a-z0-9_]+)+)")
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


# ────────────────────────────────────────────── imports DIFERIDOS (M74-E-R4)
#: Capas locales: se siguen para ampliar el cierre, no se comprueban como terceros.
LOCALES = ("vp_model", "vp_data", "pipeline", "tools", "experiments")


def _terceros_de(modulo: Path) -> set[str]:
    """TODOS los paquetes de terceros que el archivo importa, **incluidos los diferidos**."""
    import sys as _sys

    fuera: set[str] = set()
    arbol = ast.parse(modulo.read_text(encoding="utf-8"))
    for n in ast.walk(arbol):
        if isinstance(n, ast.Import):
            nombres = [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            nombres = [(n.module or "").split(".")[0]] if n.level == 0 else []
        else:
            continue
        for raiz in nombres:
            if raiz and raiz not in _sys.stdlib_module_names and raiz not in LOCALES:
                fuera.add(raiz)
    return fuera


def _cierre_local(modulo: Path, root: Path, vistos: set[Path] | None = None) -> set[Path]:
    """El archivo y, recursivamente, los módulos locales que importa **A NIVEL DE MÓDULO**."""
    vistos = vistos if vistos is not None else set()
    if modulo in vistos or not modulo.is_file():
        return vistos
    vistos.add(modulo)
    arbol = ast.parse(modulo.read_text(encoding="utf-8"))
    for n in arbol.body:
        mods: list[str] = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
        for m in mods:
            partes = m.split(".")
            if partes[0] in LOCALES:
                cand = root.joinpath(*partes).with_suffix(".py")
                if cand.is_file():
                    _cierre_local(cand, root, vistos)
                elif (root.joinpath(*partes[:-1]) / f"{partes[-1]}.py").is_file():
                    _cierre_local(root.joinpath(*partes[:-1]) / f"{partes[-1]}.py", root, vistos)
    return vistos


def deferred_imports_resolve(root: Path = ROOT) -> list[str]:
    """Cada tercero que el cierre local de un entrypoint importa, ¿existe en SU perfil?"""
    fallos: list[str] = []
    cache: dict[tuple[str, str], tuple[bool, str]] = {}
    # ⚠️ SÓLO los que un runner invoca de verdad, y con la ruta COMPLETA.
    # Dos falsos positivos míos al estrenarlo: pasé `p.name` donde `interpreter_of` empareja
    # `experiments/x.py`, así que cuatro guiones de `ante_nf` se midieron contra `ante`; y gateé
    # guiones de investigación que ningún runner llama (`improve_tabpfn`, `improve_timesfm`)
    # contra un perfil en el que nunca corren. Un gate que acusa a quien no participa es ruido, y
    # el ruido se acaba desactivando.
    # ★ El universo son los runners que la CAMPAÑA usa, derivados siguiendo los shells anidados
    # desde `run_rederivation.sh` — no todos los `.sh` del repositorio. `run_statsforecast.py` sólo
    # lo invoca el runbook histórico AQ, y hacer fallar la preparación de la campaña por un guion
    # que la campaña no ejecuta sería ruido; su hueco real se reporta aparte, no se silencia.
    pendientes = [root / "experiments" / "run_rederivation.sh"]
    shells: list[Path] = []
    while pendientes:
        sh = pendientes.pop()
        if sh in shells or not sh.is_file():
            continue
        shells.append(sh)
        for anidado in re.findall(
            r"bash\s+(experiments/[a-z0-9_]+\.sh)", sh.read_text(encoding="utf-8", errors="ignore")
        ):
            pendientes.append(root / anidado)

    invocados: dict[str, str] = {}
    for f in shells:
        if True:
            texto = f.read_text(encoding="utf-8", errors="ignore")
            for m in NF_CALL.findall(texto):
                invocados[m] = "ante_nf"
            for m in ANTE_CALL.findall(texto):
                invocados.setdefault(m, "ante")
            for mod in NF_MODULE.findall(texto):
                invocados[mod.replace(".", "/") + ".py"] = "ante_nf"
            for mod in ANTE_MODULE.findall(texto):
                invocados.setdefault(mod.replace(".", "/") + ".py", "ante")
    for script, perfil in sorted(invocados.items()):
        ruta = root / script
        if not ruta.is_file():
            continue
        interp = INTERPRETERS[perfil] if perfil in INTERPRETERS else root / perfil / "bin" / "python"
        if not interp.is_file():
            continue
        terceros: set[str] = set()
        for mod in _cierre_local(ruta, root):
            terceros |= _terceros_de(mod)
        for paquete in sorted(terceros):
            clave = (perfil, paquete)
            if clave not in cache:
                # ★ B3 de la auditoría `8656cf44…`: se IMPORTA de verdad, no `find_spec`.
                # Un paquete presente pero roto por una dependencia binaria pasaba el gate y moría
                # en ejecución; «está instalado» y «se puede usar» no son lo mismo.
                fin = subprocess.run(
                    [str(interp), "-c", f"import {paquete}"],
                    cwd=str(root),
                    env=_clean_env(root),
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                cola = (fin.stderr or fin.stdout or "").strip().splitlines()
                cache[clave] = (fin.returncode == 0, " / ".join(cola[-2:])[:200])
            ok, motivo = cache[clave]
            if not ok:
                fallos.append(f"{script} ({perfil}): `{paquete}` no se puede importar — {motivo}")
    return fallos


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

        # ★ M74-E-R4 · los DIFERIDOS, que es lo que dejó pasar `optuna`.
        for f in deferred_imports_resolve():
            fallos.append(f)
            print(f"✗ IMPORT DIFERIDO SIN RESOLVER · {f}")

    if fallos:
        print(f"\n✗ SMOKE DE ENTRYPOINTS ROTO ({len(fallos)}): se detecta en segundos lo que la campaña")
        print("  tardaría horas en descubrir. Corrige antes de lanzar nada.")
        return 1
    alcance = "símbolos cruzados resueltos" + (
        "" if args.symbols_only else " · todos arrancan en su perfil · imports DIFERIDOS resueltos"
    )
    print(f"✓ smoke de entrypoints OK: {alcance}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
