"""¿El intérprete que va a correr la campaña REPRODUCE su lock? Medido, no declarado.

El preflight sellaba `locks/model-cpu.txt` y `locks/deep-macos-arm64.txt` y comprobaba que los
directorios `ante/` y `ante_nf/` **existieran**. Nada más. Es decir: sellaba la *declaración* del
entorno y nunca el entorno. Medido el 13-sep-2026, la distancia entre una cosa y la otra era esta:

    ante     vs locks/model-cpu.txt        →  3 pines ausentes, 24 a otra versión
    ante_nf  vs locks/deep-macos-arm64.txt → 18 pines ausentes, 32 a otra versión

y entre ellas **`torch` 2.13.0 en el lock contra 2.12.0 instalado** en los dos intérpretes, más
`numba`/`llvmlite` —que compilan al vuelo el núcleo de statsforecast— y `coreforecast`. Una campaña
de once horas lanzada así produce cifras que el lock **no reproduce**, y el recibo las habría
presentado como selladas. Es exactamente el defecto que este lote persigue: declarar sin verificar.

**Criterio, y por qué está partido en dos:**

* **BLOQUEANTE** — un pin del lock ausente o instalado a otra versión. El lock dice qué entorno
  produce estas cifras; si no está, no las produce.
* **BLOQUEANTE también** — cualquier distribución instalada que el lock no nombre y que no esté
  **declarada por nombre** en `UNGOVERNED_OK`.

  ★ **M74-E-R4 corrige aquí un criterio mío que era un agujero.** R2 clasificó los extras como
  *informativos*, razonando con `dvc` y `mlflow`, que son **herramientas**. Pero `optuna`
  **participa en el cálculo**, y esa distinción no estaba en el gate: instalar optuna a mano
  habría dejado `reproduces_lock: true` con un paquete ungobernado dentro del HPO. Ahora lo no
  declarado bloquea, y la lista de excepciones es explícita, versionada y corta — si crece, se ve
  en el diff. Un extra no declarado deja de ser ruido tolerado y pasa a ser una decisión.

⚠️ **Sin bypass.** Un `--skip-…` aquí sería el mismo agujero que M74-B-R1 tuvo que arrancar de
raíz. La salida es arreglar el entorno, no saltarse la comprobación.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: ★ AUTORIDAD ÚNICA de qué lock gobierna a qué intérprete. `tools/campaign_preflight.py` la
#: importa de aquí: tener la pareja escrita en dos sitios es como acaban divergiendo.
#: `locks/runtime.txt` no gobierna a ninguno: el runbook llama a `ante/bin/python` y a
#: `ante_nf/bin/python`, cada uno con su perfil (H4).
INTERPRETER_LOCKS: tuple[tuple[str, str], ...] = (
    ("ante", "locks/model-cpu.txt"),
    ("ante_nf", "locks/deep-macos-arm64.txt"),
)

#: Distribuciones que pueden estar sin gobernar por el lock, **declaradas por intérprete**.
#: `pip` es el instalador; `visapredictai` es el propio proyecto en editable, que por definición no
#: puede pinnearse a sí mismo. Cualquier otra cosa bloquea.
UNGOVERNED_OK: dict[str, frozenset[str]] = {
    "ante": frozenset({"pip", "visapredictai"}),
    "ante_nf": frozenset({"pip"}),
}

_PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;]+)")
#: Programa que se ejecuta DENTRO del intérprete objetivo. `importlib.metadata` va en la biblioteca
#: estándar: no exige que `pip` esté instalado ni toca la red.
_CENSO = (
    "import json,importlib.metadata as m;"
    "print(json.dumps({(d.metadata['Name'] or '').lower().replace('_','-'): d.version "
    "for d in m.distributions() if d.metadata['Name']}))"
)


class EnvLockError(RuntimeError):
    """El intérprete no reproduce el lock que lo gobierna."""


def normalizar(nombre: str) -> str:
    """PEP 503: `Foo_Bar` y `foo-bar` son el mismo proyecto y no pueden contarse como dos."""
    return re.sub(r"[-_.]+", "-", nombre).lower()


def pines_del_lock(ruta: Path) -> dict[str, str]:
    """Los `nombre==versión` del lock. Los comentarios y los hashes no son pines."""
    pines: dict[str, str] = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        m = _PIN.match(linea.split("#")[0].strip())
        if m:
            pines[normalizar(m.group(1))] = m.group(2)
    if not pines:
        raise EnvLockError(f"{ruta} no declara un solo pin: un lock vacío no gobierna nada")
    return pines


def instalado_en(venv: Path) -> dict[str, str]:
    """Lo que el intérprete tiene DE VERDAD, preguntándoselo a él."""
    py = venv / "bin" / "python"
    if not py.exists():
        raise EnvLockError(f"{py} no existe: no hay intérprete que verificar")
    # ⚠️ `cwd=venv`, no la raíz del repositorio. Heredando el cwd, `importlib.metadata` descubre el
    # `visapredictai.egg-info` que el install editable deja en la RAÍZ, y el proyecto aparecía como
    # extra en LOS DOS intérpretes — incluido `ante_nf`, donde no está instalado. Con los extras ya
    # bloqueantes, ese falso positivo rompería el gate: el censo tiene que medir el INTÉRPRETE.
    fin = subprocess.run([str(py), "-c", _CENSO], cwd=str(venv), capture_output=True, text=True, timeout=180)
    if fin.returncode != 0:
        raise EnvLockError(f"{py} no pudo censar sus distribuciones: {fin.stderr.strip()[:200]}")
    return {normalizar(k): v for k, v in json.loads(fin.stdout).items()}


def comparar(venv: Path, lock: Path) -> dict:
    """El veredicto de un intérprete. Determinista: todo ordenado, nada dependiente del orden de pip."""
    pines, real = pines_del_lock(lock), instalado_en(venv)
    ausentes = sorted(set(pines) - set(real))
    difieren = sorted(k for k in set(pines) & set(real) if pines[k] != real[k])
    extras = sorted(set(real) - set(pines))
    return {
        "venv": venv.name,
        "lock": lock.as_posix() if not lock.is_absolute() else lock.name,
        "n_pins": len(pines),
        "n_installed": len(real),
        "missing": [{"name": k, "locked": pines[k]} for k in ausentes],
        "version_mismatch": [{"name": k, "locked": pines[k], "installed": real[k]} for k in difieren],
        "extra": extras,
        "undeclared": [k for k in extras if k not in UNGOVERNED_OK.get(venv.name, frozenset())],
        "reproduces_lock": not ausentes
        and not difieren
        and not [k for k in extras if k not in UNGOVERNED_OK.get(venv.name, frozenset())],
    }


def auditar(root: Path = ROOT) -> dict[str, dict]:
    """Todos los intérpretes que la campaña usa, en el orden declarado."""
    return {venv: comparar(root / venv, root / lock) for venv, lock in INTERPRETER_LOCKS}


def exigir_reproducibles(root: Path = ROOT) -> dict[str, dict]:
    """Fail-closed: devuelve el veredicto o levanta nombrando lo que hay que arreglar."""
    veredicto = auditar(root)
    rotos = {k: v for k, v in veredicto.items() if not v["reproduces_lock"]}
    if rotos:
        detalle = "; ".join(
            f"{k}: {len(v['missing'])} ausente(s), {len(v['version_mismatch'])} a otra versión "
            f"(p. ej. {', '.join(d['name'] for d in (v['version_mismatch'] or v['missing'])[:3])})"
            for k, v in sorted(rotos.items())
        )
        raise EnvLockError(
            "el o los intérpretes de la campaña NO reproducen su lock — " + detalle + ". "
            "Las cifras que produzca esta corrida no serán reproducibles desde locks/. "
            "Reconstruye el entorno desde su lock; no hay forma de saltarse esta comprobación."
        )
    return veredicto


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verifica que cada intérprete reproduzca su lock.")
    ap.add_argument("--json", action="store_true", help="veredicto completo, legible por máquina")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    veredicto = auditar(Path(args.root).resolve())

    if args.json:
        print(json.dumps(veredicto, indent=2, sort_keys=True))
    else:
        for venv, v in veredicto.items():
            marca = "✓" if v["reproduces_lock"] else "✗"
            print(f"\n{marca} {venv}  vs  {v['lock']}   ({v['n_pins']} pines · {v['n_installed']} instalados)")
            for d in v["missing"]:
                print(f"    AUSENTE          {d['name']:28s} lock={d['locked']}")
            for d in v["version_mismatch"]:
                print(f"    OTRA VERSIÓN     {d['name']:28s} lock={d['locked']:14s} real={d['installed']}")
            for nombre in v["undeclared"]:
                print(f"    SIN GOBERNAR     {nombre:28s} ni en el lock ni en UNGOVERNED_OK")
            declarados = [e for e in v["extra"] if e not in v["undeclared"]]
            if declarados:
                print(f"    (declarados fuera del lock) {', '.join(declarados)}")

    rotos = [k for k, v in veredicto.items() if not v["reproduces_lock"]]
    if rotos:
        print(
            f"\n✗ {', '.join(rotos)} NO reproduce(n) su lock. Una campaña lanzada así produce cifras "
            "que locks/ no puede reconstruir.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
