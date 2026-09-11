"""Sella las ENTRADAS de una campaña y reconcilia su protocolo, antes de calcular nada.

La campaña causal (#33) re-deriva todas las cifras retrospectivas del proyecto. Lo que decide si
su resultado es creíble no es la corrida: es **qué entró** y **bajo qué reglas**, sellado *antes*
de mirar un número. Este módulo produce esas dos cosas y nada más — **no ejecuta la campaña, no
escribe en `reports/` y no toca artefactos publicados**.

**Las entradas se DERIVAN, no se listan a mano** (un inventario escrito a mano envejece):

* ``code`` — el cierre transitivo de imports locales a partir de los entrypoints que el runbook
  invoca, leídos del propio ``run_rederivation.sh`` por AST y regex, más los guiones de shell.
* ``data`` — las salidas *git-only* de los stages de datos del DAG, tomadas de ``dvc.yaml``.
* ``governance`` — la configuración que la campaña **lee y no reescribe**. Ésta sí se **declara
  por nombre**, y cada entrada queda **anclada a la constante del módulo que la nombra**: así, si
  alguien mueve `champion.MANIFEST`, el ancla falla en vez de sellar un archivo que ya nadie lee.
  (La lección de M72-R2: lo que se tolera o se sella se declara por identidad, no por forma.)

El ``protocol`` no se teclea: se lee de sus autoridades vivas (`vp_model.config`,
`vp_model.stability`, `vp_model.deck`, …) y se emite tal cual, para que el recibo de la campaña
pueda compararse contra él y cualquier deriva salte.

Uso:
    python tools/campaign_preflight.py --out <ruta.json>     # sella y reconcilia
    python tools/campaign_preflight.py --print               # sólo imprime la reconciliación
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "experiments" / "run_rederivation.sh"
#: Capas cuyo import se sigue en el cierre; lo demás es tercero y lo gobiernan los locks.
LOCAL_LAYERS = ("vp_model", "vp_data", "pipeline", "tools", "experiments")

#: Configuración que la campaña LEE y no reescribe, declarada por nombre y anclada al módulo que
#: la nombra: ``(ruta relativa, módulo, atributo)``. El ancla se verifica en cada sellado.
GOVERNANCE_INPUTS: tuple[tuple[str, str, str], ...] = (
    ("reports/governance/champion_manifest.json", "vp_model.champion", "MANIFEST"),
    ("docs/challenger_deck.json", "vp_model.champion", "DECK_PATH"),
    ("docs/cohort_deck.json", "vp_model.deck", "DECK_PATH"),
)
#: Entradas sin constante que las nombre (prosa normativa y cierre de dependencias del runtime).
GOVERNANCE_PLAIN: tuple[str, ...] = (
    "docs/COHORT_POLICY.md",
    "locks/runtime.txt",
    "dvc.lock",
    "dvc.yaml",
)


# --------------------------------------------------------------------------- utilidades
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def sha256_tree(root: Path) -> tuple[str, int]:
    """Huella de un directorio: sha256 sobre ``ruta\\0sha`` de cada archivo, en orden. Y su conteo."""
    h = hashlib.sha256()
    n = 0
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(str(p.relative_to(root)).encode() + b"\0" + sha256_file(p).encode() + b"\n")
        n += 1
    return h.hexdigest(), n


class PreflightError(RuntimeError):
    """Cualquier cosa que impida sellar con confianza. Siempre fail-closed."""


# --------------------------------------------------------------------------- entradas: código
def runbook_entrypoints(runbook: Path = RUNBOOK) -> dict[str, list[str]]:
    """Entrypoints que el runbook invoca, leídos del guion y **sin contar sus comentarios**."""
    if not runbook.is_file():
        raise PreflightError(f"no existe el runbook {runbook}")
    codigo = "\n".join(linea.split("#", 1)[0] for linea in runbook.read_text(encoding="utf-8").splitlines())
    return {
        "scripts": sorted(set(re.findall(r"experiments/([a-z0-9_]+\.py)", codigo))),
        "modules": sorted(set(re.findall(r"-m\s+((?:vp_model|pipeline|tools)\.[a-z0-9_.]+)", codigo))),
        "shell": sorted(set(re.findall(r"bash\s+experiments/([a-z0-9_]+\.sh)", codigo))),
    }


def _module_path(module: str) -> Path | None:
    p = ROOT / (module.replace(".", "/") + ".py")
    return p if p.is_file() else None


def code_closure(entrypoints: Mapping[str, Iterable[str]]) -> list[str]:
    """Cierre transitivo de imports LOCALES desde los entrypoints. Rutas relativas, ordenadas."""
    pendientes = [f"experiments.{n[:-3]}" for n in entrypoints["scripts"]] + list(entrypoints["modules"])
    encontrados: dict[str, Path] = {}
    while pendientes:
        modulo = pendientes.pop()
        ruta = _module_path(modulo)
        if modulo in encontrados or ruta is None:
            continue
        encontrados[modulo] = ruta
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.module.split(".")[0] in LOCAL_LAYERS:
                pendientes.append(nodo.module)
                # `from vp_model import champion` importa un MÓDULO, no un símbolo
                pendientes += [f"{nodo.module}.{a.name}" for a in nodo.names if _module_path(f"{nodo.module}.{a.name}")]
            elif isinstance(nodo, ast.Import):
                pendientes += [a.name for a in nodo.names if a.name.split(".")[0] in LOCAL_LAYERS]
    rutas = [str(ruta.relative_to(ROOT)) for ruta in encontrados.values()]
    rutas += [f"experiments/{n}" for n in entrypoints["shell"]] + [str(RUNBOOK.relative_to(ROOT))]
    return sorted(set(rutas))


# --------------------------------------------------------------------------- entradas: datos
def data_inputs(dag: Mapping[str, Any]) -> list[str]:
    """Salidas *git-only* de los stages de datos, tomadas del DAG (no escritas a mano)."""
    fuera: set[str] = set()
    for stage in ("scrape", "panel", "bulletins"):
        for out in dag["stages"][stage].get("outs", []):
            fuera.add(next(iter(out)) if isinstance(out, dict) else out)
    return sorted(fuera)


# --------------------------------------------------------------------------- protocolo
def _deck_summary(d: Any) -> dict[str, Any]:
    """Resumen del deck de cohortes CONGELADO (E3): qué recetas existen, no sus resultados.

    ⚠️ `primarias` y `controles` son MÉTODOS del deck, no atributos: se invocan. Serializar el
    objeto ligado habría reventado el sellado, que es justo lo que pasó la primera vez.
    """

    def normaliza(v: Any) -> Any:
        if callable(v):
            v = v()
        if isinstance(v, (set, frozenset)):
            return sorted(v)
        if isinstance(v, dict):
            return sorted(v)
        if isinstance(v, (tuple, list)):
            return [x if isinstance(x, (str, int, float, bool)) or x is None else getattr(x, "name", str(x)) for x in v]
        return v

    campos = ("cohorts", "primarias", "controles", "seeds", "device", "frozen", "max_primary_per_cohort")
    return {campo: normaliza(getattr(d, campo)) for campo in campos if hasattr(d, campo)}


def reconcile_protocol() -> dict[str, Any]:
    """Lee el protocolo de sus AUTORIDADES vivas. Nada aquí está tecleado."""
    sys.path.insert(0, str(ROOT))
    from vp_model import config, deck, stability  # import diferido: este módulo corre en el job base

    # `run_metadata()` es la autoridad viva del protocolo: la misma que estampan los ledgers.
    # ⚠️ Se le retiran los campos VOLÁTILES (sha de git, timestamp, run_id, campaign_id, dirty),
    # que cambian en cada invocación: sellarlos haría que dos preflights idénticos difirieran.
    meta = config.run_metadata()
    volatiles = {"git_sha", "git_dirty", "timestamp", "run_id", "campaign_id"}
    protocolo: dict[str, Any] = {
        "run_metadata": {k: v for k, v in meta.items() if k not in volatiles},
        "tables": list(config.TABLES),
        "pilot_countries": list(config.PILOT_COUNTRIES),
        "horizons": list(config.HORIZONS),
        "horizon_candidates": list(config.HORIZON_CANDIDATES),
        "min_trainable_evaluable": config.MIN_TRAINABLE_EVALUABLE,
        "gap_policy": meta["features"]["gap_policy"],
        "mask_covariates": {k: list(v) for k, v in sorted(config.MASK_COVARIATES.items())},
        "cohorts": {
            "rule_version": stability.RULE_VERSION,
            "names": sorted(stability.COHORTES),
        },
        "cohort_deck": _deck_summary(deck.cargar_deck()),
    }
    return protocolo


# --------------------------------------------------------------------------- sellado
def seal(root: Path = ROOT) -> dict[str, Any]:
    """Construye el sello completo. Fail-closed ante cualquier entrada ausente o ancla rota."""
    import yaml

    dag = yaml.safe_load((root / "dvc.yaml").read_text(encoding="utf-8"))
    entrypoints = runbook_entrypoints()
    faltantes: list[str] = []

    def hashes(rutas: Iterable[str]) -> dict[str, str]:
        fuera: dict[str, str] = {}
        for rel in rutas:
            p = root / rel
            if p.is_file():
                fuera[rel] = sha256_file(p)
            elif p.is_dir():
                digest, n = sha256_tree(p)
                fuera[rel] = f"tree:{digest}:{n}"
            else:
                faltantes.append(rel)
        return fuera

    codigo = hashes(code_closure(entrypoints))
    datos = hashes(data_inputs(dag))

    # gobernanza: además de sellar, se comprueba el ANCLA de cada entrada declarada
    sys.path.insert(0, str(root))
    import importlib

    anclas: dict[str, str] = {}
    for rel, modulo, atributo in GOVERNANCE_INPUTS:
        objetivo = getattr(importlib.import_module(modulo), atributo, None)
        if objetivo is None:
            faltantes.append(f"{rel} (ancla {modulo}.{atributo} inexistente)")
            continue
        real = Path(objetivo).resolve()
        if real != (root / rel).resolve():
            faltantes.append(f"{rel} (ancla {modulo}.{atributo} apunta a {real})")
        anclas[rel] = f"{modulo}.{atributo}"
    gobernanza = hashes([rel for rel, _, _ in GOVERNANCE_INPUTS] + list(GOVERNANCE_PLAIN))

    if faltantes:
        raise PreflightError("entradas ausentes o anclas rotas: " + "; ".join(sorted(faltantes)))

    cabeza = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    sucio = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()

    return {
        "schema_version": 1,
        "purpose": "sello de entradas y protocolo de la campaña causal (#33), previo a calcular",
        "git": {"head": cabeza, "dirty": bool(sucio)},
        "entrypoints": entrypoints,
        "inputs": {
            "code": codigo,
            "data": datos,
            "governance": gobernanza,
            "governance_anchors": anclas,
        },
        "counts": {"code": len(codigo), "data": len(datos), "governance": len(gobernanza)},
        "protocol": reconcile_protocol(),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="campaign_preflight", description=__doc__.split("\n")[0])
    p.add_argument("--out", help="ruta del sello JSON (si se omite, no se escribe nada)")
    p.add_argument("--print", dest="mostrar", action="store_true", help="imprime la reconciliación")
    args = p.parse_args(argv)
    try:
        sello = seal()
    except (PreflightError, OSError, ImportError) as exc:
        print(f"✗ preflight de campaña: {exc}", file=sys.stderr)
        return 1
    texto = json.dumps(sello, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(texto, encoding="utf-8")
        print(f"✓ entradas selladas → {args.out}")
    if args.mostrar or not args.out:
        print(texto)
    c = sello["counts"]
    print(
        f"✓ preflight: {c['code']} archivos de código · {c['data']} de datos · "
        f"{c['governance']} de gobernanza · HEAD {sello['git']['head'][:9]}"
        f"{' (ÁRBOL SUCIO)' if sello['git']['dirty'] else ''}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
