"""Sella las ENTRADAS de una campaña y reconcilia su protocolo, antes de calcular nada.

La campaña causal (#33) re-deriva todas las cifras retrospectivas del proyecto. Lo que decide si
su resultado es creíble no es la corrida: es **qué entró** y **bajo qué reglas**, sellado *antes*
de mirar un número. Este módulo produce esas dos cosas y nada más — **no ejecuta la campaña, no
escribe en `reports/` y no toca artefactos publicados**.

**Las entradas se DERIVAN, no se listan a mano** (un inventario escrito a mano envejece):

* ``code`` — el cierre transitivo de imports locales a partir de los entrypoints que el runbook
  invoca, **siguiendo recursivamente los shells anidados**, más los propios guiones.
  ⚠️ El hash de cada archivo es sobre sus **bytes**: los comentarios se descartan al **descubrir**
  entrypoints, no al hashear. Un cambio de comentario cambia el sello, y eso es lo conservador.
* ``data`` — las salidas *git-only* de los stages de datos del DAG, tomadas de ``dvc.yaml``.
* ``governance`` — la configuración que la campaña **lee**, declarada **por nombre**. Tres entradas
  están **ancladas a la constante del módulo que las nombra** (si alguien mueve
  `champion.MANIFEST`, el ancla falla en vez de sellar un archivo que ya nadie lee); las demás son
  **planas**, porque ninguna constante las nombra: `dvc.yaml`/`dvc.lock`, la política de cohortes,
  y las entradas mutables que gobiernan etapas (`tuned_params`, `schema.sql`,
  `pipeline/migrations`, `consistency_rules.yml`) más los **locks reales de cada intérprete**.

El ``protocol`` no se teclea: se lee de sus autoridades vivas (`vp_model.config`,
`vp_model.stability`, `vp_model.deck`, …) y se emite tal cual, para que el recibo de la campaña
pueda compararse contra él y cualquier deriva salte.

Uso:
    python -m tools.campaign_preflight --out <ruta.json>     # sella y reconcilia
    python -m tools.campaign_preflight --print               # sólo imprime la reconciliación

⚠️ Se invoca como MÓDULO, no como guion: ejecutar `python tools/campaign_preflight.py` deja `tools`
fuera del `sys.path` y el import del verificador de entorno revienta. Es la convención que ya usan
`sync_all.sh` y el runbook con `tools.campaign_txn`; escribirla aquí evita repetir la trampa de
M41-R1 y M60 con un `try/except` que sólo la disimularía.
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

from tools import check_env_matches_lock as _envlock

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
    "dvc.lock",
    "dvc.yaml",
    # H4 · entradas MUTABLES que gobiernan etapas y estaban fuera del sello:
    "reports/eval/tuned_params.json",  # parametriza el pool de la etapa 1 y lo reescribe la 6
    "schema.sql",  # gobierna la etapa 0 (almacén)
    "pipeline/migrations",  # idem, como árbol
    "tools/consistency_rules.yml",  # decide la etapa 10, incluido `retro_protocol`
    "locks/lockset.json",  # ★ M74-E-R8 · la procedencia de la matriz que construyó los entornos
)
#: Locks REALES de cada intérprete que la campaña usa. ★ La pareja vive en
#: `tools/check_env_matches_lock.py`, que es quien la comprueba: tenerla escrita aquí TAMBIÉN era
#: dejar que las dos copias divergieran sin que nada lo notara. Se reexporta porque varias pruebas
#: y el propio sello la nombran por este camino.
INTERPRETER_LOCKS = _envlock.INTERPRETER_LOCKS


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
#: Cómo se invoca un ejecutable en estos guiones. Se cubren las formas REALES medidas en el
#: árbol: `$ANTE x.py`, `$NF x.py`, `ante_nf/bin/python x.py`, `python tools/x.py` y `-m paquete.mod`.
SCRIPT_CALL = re.compile(r"(?:^|\s)((?:experiments|tools|pipeline)/[a-z0-9_]+\.py)")
MODULE_CALL = re.compile(r"-m\s+((?:vp_model|pipeline|tools|experiments)\.[a-z0-9_.]+)")
SHELL_CALL = re.compile(r"(?:^|\s)(?:bash|zsh|sh)\s+((?:experiments|tools)/[a-z0-9_]+\.sh)")


def _strip_comments(texto: str) -> str:
    """Quita los comentarios de un guion de shell: lo que está comentado no se ejecuta."""
    return "\n".join(linea.split("#", 1)[0] for linea in texto.splitlines())


def runbook_entrypoints(runbook: Path = RUNBOOK) -> dict[str, list[str]]:
    """Entrypoints que el runbook invoca, **siguiendo recursivamente los shells anidados**.

    ⚠️ La primera versión leía SÓLO el guion de arriba y sellaba 54 archivos cuando la campaña
    ejecuta 66: `run_campaign.sh`, `save_finalists.sh` y `sync_all.sh` invocan a su vez
    `run_comparison`, `run_global_deep`, `aggregate_seeds`, `save_finalists_deep`,
    `export_forecasts`, `sync_mlflow`… Ninguno entraba al sello, así que el pool F1 o la campaña
    deep podían alterarse sin que el recibo lo delatara (H1 de la auditoría ciega).
    """
    if not runbook.is_file():
        raise PreflightError(f"no existe el runbook {runbook}")
    scripts: set[str] = set()
    modules: set[str] = set()
    shells: set[str] = set()
    pendientes = [runbook]
    vistos: set[Path] = set()
    while pendientes:
        actual = pendientes.pop()
        if actual in vistos or not actual.is_file():
            continue
        vistos.add(actual)
        codigo = _strip_comments(actual.read_text(encoding="utf-8"))
        scripts |= set(SCRIPT_CALL.findall(codigo))
        modules |= set(MODULE_CALL.findall(codigo))
        for rel in SHELL_CALL.findall(codigo):
            shells.add(rel)
            pendientes.append(ROOT / rel)
    return {
        "scripts": sorted(scripts),
        "modules": sorted(modules),
        "shell": sorted(shells),
    }


def _module_path(module: str) -> Path | None:
    p = ROOT / (module.replace(".", "/") + ".py")
    return p if p.is_file() else None


def code_closure(entrypoints: Mapping[str, Iterable[str]]) -> list[str]:
    """Cierre transitivo de imports LOCALES desde los entrypoints. Rutas relativas, ordenadas."""
    # `scripts` ya trae rutas relativas completas (`experiments/x.py`, `tools/y.py`)
    pendientes = [n[:-3].replace("/", ".") for n in entrypoints["scripts"]] + list(entrypoints["modules"])
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
            # M74-E-R6 · imports PLANOS entre hermanos (`import seed_coverage`, `from hpo_winner_receipt
            # import …`): no empiezan por una capa local y dejaban fuera del sello, entre otros, el
            # recibo de la ganadora del HPO. Se resuelven contra el directorio del importador.
            if isinstance(nodo, (ast.Import, ast.ImportFrom)) and getattr(nodo, "level", 0) == 0:
                planos = [a.name for a in nodo.names] if isinstance(nodo, ast.Import) else [nodo.module or ""]
                for nombre in planos:
                    hermano = ruta.parent / f"{nombre}.py"
                    if "." not in nombre and nombre.split(".")[0] not in LOCAL_LAYERS and hermano.is_file():
                        pendientes.append(str(hermano.relative_to(ROOT))[:-3].replace("/", "."))
    rutas = [str(ruta.relative_to(ROOT)) for ruta in encontrados.values()]
    rutas += list(entrypoints["shell"]) + [str(RUNBOOK.relative_to(ROOT))]
    return sorted(set(rutas))


# --------------------------------------------------------------------------- entradas: datos
#: La entrada CRUDA del pipeline: los boletines congelados. Es **dependencia** del stage `scrape`,
#: no salida de ninguno, así que no aparecía por el DAG y quedaba fuera del sello (H4).
RAW_SNAPSHOTS = "data/snapshots"


def data_inputs(dag: Mapping[str, Any]) -> list[str]:
    """Entradas de datos: las salidas *git-only* de los stages de datos, **más el congelado crudo**."""
    fuera: set[str] = {RAW_SNAPSHOTS}
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

    from tools import lock_contracts

    # ★ M74-E-R8 · la procedencia de los locks es PRECONDICIÓN del sello (auditoría `e6c76896…`): un
    # preflight verde y byte-idéntico sobre un checkout cuyo contrato falla no sella nada que sirva.
    procedencia = lock_contracts.validate_all(root)
    if procedencia:
        raise PreflightError("contrato de locks incumplido: " + "; ".join(procedencia))

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
    # los locks REALES: uno por intérprete, anclado al venv que la campaña usa de verdad
    locks: list[str] = []
    for venv, lock in INTERPRETER_LOCKS:
        if not (root / venv).exists():
            faltantes.append(f"{venv}/ (intérprete de la campaña ausente)")
        locks.append(lock)
    gobernanza = hashes([rel for rel, _, _ in GOVERNANCE_INPUTS] + list(GOVERNANCE_PLAIN) + locks)

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
        # ★ M74-E: el sello MIDE el entorno, no lo supone. Hasta aquí hasheaba los ARCHIVOS de lock
        # y comprobaba que los directorios de los venv existieran — es decir, sellaba la
        # declaración del entorno y jamás el entorno. Medido el 13-sep-2026, los dos intérpretes
        # corrían `torch` 2.12.0 contra un lock que sella 2.13.0. El veredicto entra EN el sello:
        # una campaña sellada sobre un entorno divergente lo lleva escrito para siempre, y el
        # runbook se niega a arrancar (`run_rederivation.sh`), que es donde se pierden las 11 horas.
        "environment": _envlock.auditar(root),
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
