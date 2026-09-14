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
import hashlib
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
#: ★ M74-E-R5 (B2) · lo que hace falta para ACREDITAR las dos excepciones, no sólo nombrarlas:
#: de dónde viene el editable y a qué resuelve `vp_model`. Se pregunta al propio intérprete.
_ACREDITACION = (
    "import json,importlib.metadata as m\n"
    "out={'pip':None,'direct_url':None,'vp_model':None}\n"
    "try: out['pip']=m.version('pip')\n"
    "except Exception: pass\n"
    "try: out['direct_url']=json.loads(m.distribution('visapredictai').read_text('direct_url.json') or '{}')\n"
    "except Exception: pass\n"
    "try:\n"
    "    import vp_model; out['vp_model']=vp_model.__file__\n"
    "except Exception: pass\n"
    "print(json.dumps(out))"
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
    # ⚠️ ABSOLUTO antes de cambiar el cwd. Con `cwd=venv` y una ruta relativa (`ante/bin/python`),
    # el intérprete se resolvía contra el nuevo directorio y salía `FileNotFoundError`. Lo cazó una
    # llamada mía con `Path("ante")`; en `auditar()` no salía porque allí la ruta ya viene compuesta.
    venv = venv.resolve()
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


def acreditar_excepciones(venv: Path, root: Path, toolchain: dict[str, str], extras: list[str]) -> list[str]:
    """Las dos excepciones de `UNGOVERNED_OK` se ACREDITAN, no se creen por su nombre.

    ★ B2 de la auditoría `8656cf44…`: permitir `pip` y `visapredictai` por nombre aceptaba
    igualmente **otro** `visapredictai==1.0.0` o un editable apuntando a otro checkout, y no decía
    nada de la versión de `pip` frente al toolchain sellado. Un permiso por nombre es un permiso a
    cualquiera que se llame así.

    Se exige: `pip` == el del `lockset`; y si el proyecto está presente, que sea **editable**, que
    su `direct_url.json` apunte a ESTE worktree y que `vp_model` resuelva bajo él.
    """
    problemas: list[str] = []
    py = venv / "bin" / "python"
    fin = subprocess.run([str(py), "-c", _ACREDITACION], cwd=str(venv), capture_output=True, text=True, timeout=180)
    if fin.returncode != 0:
        return [f"{venv.name}: no se pudo acreditar el entorno ({fin.stderr.strip()[:160]})"]
    datos = json.loads(fin.stdout)

    esperado_pip = str(toolchain.get("pip", ""))
    if esperado_pip and datos.get("pip") != esperado_pip:
        problemas.append(f"{venv.name}: pip {datos.get('pip')!r} ≠ el del lockset {esperado_pip!r}")

    if "visapredictai" in extras:
        du = datos.get("direct_url") or {}
        if not (du.get("dir_info") or {}).get("editable"):
            problemas.append(f"{venv.name}: `visapredictai` no está instalado como EDITABLE ({du or 'sin direct_url'})")
        url = str(du.get("url", ""))
        real = Path(root).resolve()
        if not url.startswith("file://") or Path(url[7:]).resolve() != real:
            problemas.append(f"{venv.name}: el editable apunta a {url!r} y este worktree es {real}")
        vpm = datos.get("vp_model")
        if not vpm or real not in Path(vpm).resolve().parents:
            problemas.append(f"{venv.name}: `vp_model` resuelve a {vpm!r}, fuera de {real}")
    return problemas


def comparar(venv: Path, lock: Path) -> dict:
    """El veredicto de un intérprete. Determinista: todo ordenado, nada dependiente del orden de pip."""
    pines, real = pines_del_lock(lock), instalado_en(venv)
    ausentes = sorted(set(pines) - set(real))
    difieren = sorted(k for k in set(pines) & set(real) if pines[k] != real[k])
    extras = sorted(set(real) - set(pines))
    # ★ B5 de la auditoría `8656cf44…`: el preflight guardaba el VEREDICTO contra los locks, pero
    # no el inventario. Se sella el `freeze` completo por hash —un número que cambia si cambia
    # cualquier versión— y las versiones de los extras permitidos, que hasta ahora sólo aparecían
    # por su nombre. Sin esto, «reproduces_lock: true» no decía CON QUÉ se corrió.
    freeze_sha = hashlib.sha256("\n".join(f"{k}=={real[k]}" for k in sorted(real)).encode()).hexdigest()
    return {
        "venv": venv.name,
        "lock": lock.as_posix() if not lock.is_absolute() else lock.name,
        "n_pins": len(pines),
        "n_installed": len(real),
        "missing": [{"name": k, "locked": pines[k]} for k in ausentes],
        "version_mismatch": [{"name": k, "locked": pines[k], "installed": real[k]} for k in difieren],
        "extra": extras,
        "extra_versions": {k: real[k] for k in extras},
        "freeze_sha256": freeze_sha,
        "undeclared": [k for k in extras if k not in UNGOVERNED_OK.get(venv.name, frozenset())],
        "reproduces_lock": not ausentes
        and not difieren
        and not [k for k in extras if k not in UNGOVERNED_OK.get(venv.name, frozenset())],
    }


def _toolchain(root: Path) -> dict[str, str]:
    ruta = Path(root) / "locks" / "lockset.json"
    if not ruta.is_file():
        return {}
    return dict(json.loads(ruta.read_text(encoding="utf-8")).get("generator") or {})


def auditar(root: Path = ROOT) -> dict[str, dict]:
    """Todos los intérpretes que la campaña usa, en el orden declarado."""
    tc = _toolchain(root)
    salida: dict[str, dict] = {}
    for venv, lock in INTERPRETER_LOCKS:
        r = comparar(root / venv, root / lock)
        r["accreditation"] = acreditar_excepciones(root / venv, root, tc, r["extra"])
        r["reproduces_lock"] = r["reproduces_lock"] and not r["accreditation"]
        salida[venv] = r
    return salida


def exigir_reproducibles(root: Path = ROOT) -> dict[str, dict]:
    """Fail-closed: devuelve el veredicto o levanta nombrando lo que hay que arreglar."""
    veredicto = auditar(root)
    rotos = {k: v for k, v in veredicto.items() if not v["reproduces_lock"]}
    if rotos:
        detalle = "; ".join(
            # ⚠️ Conteos Y NOMBRES. Al reescribir este mensaje dejé sólo los conteos y una prueba
            # lo cazó: «1 a otra versión» sin decir cuál obliga a ir a buscarlo, y un gate que
            # obliga a investigar para entenderlo se lee por encima.
            f"{k}: {len(v['missing'])} ausente(s), {len(v['version_mismatch'])} a otra versión, "
            f"{len(v['undeclared'])} sin gobernar, {len(v.get('accreditation', []))} sin acreditar"
            + (
                f" (p. ej. {', '.join(d['name'] for d in (v['version_mismatch'] or v['missing'])[:3])})"
                if (v["version_mismatch"] or v["missing"])
                else ""
            )
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
            for problema in v.get("accreditation", []):
                print(f"    SIN ACREDITAR    {problema}")
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
