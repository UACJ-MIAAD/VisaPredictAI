"""E3 · La baraja pre-registrada de recetas: qué es primario, qué es control, qué está congelado.

Una campaña que puede elegir su receta después de ver el resultado no mide nada. Por eso la
baraja vive en ``docs/cohort_deck.json``, se **commitea antes de entrenar** y este módulo es la
única puerta para leerla: el corredor no acepta una receta que no esté declarada, y no hay forma
de ascender un control a primario sin cambiar el archivo pre-registrado (lo que deja rastro en
la historia del repositorio).

Sólo stdlib: el corredor profundo vive en un entorno aislado que no trae el resto del producto.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DECK_PATH", "DEFAULT_RUNNER", "Deck", "LaneSpec", "Receta", "cargar_deck"]

DECK_PATH = Path(__file__).resolve().parent.parent / "docs" / "cohort_deck.json"
#: Cómo se resuelve cada clave de `inputs` a un archivo del árbol (las demás son rutas relativas tal cual).
RUTAS_ENTRADAS = {
    "visa_panel_long.csv": "data/processed/visa_panel_long.csv",
    "visa_panel_long.parquet": "data/processed/visa_panel_long.parquet",
    "cohort_scan.json": "reports/eval/cohort_scan.json",
    "series_cohorts.json": "reports/eval/series_cohorts.json",
}
#: ★ M74-F · deuda 7 — entradas que la campaña RE-DERIVA en la etapa 8c antes de correr E3 (#58,
#: opción A: `build_cohorts` y `scan_cohorts` dentro de la misma transacción). Su hash declarado es
#: la base del pre-registro, no lo que consume la campaña: en la cuarta campaña real `cohort_scan.json`
#: pasó de `806b3fed…` (declarado) a `9dc3cf12…` (vivo) porque reescribe sus scorecards. Se DELIMITAN
#: (ambos hashes quedan registrados); las demás son congeladas y tienen que coincidir.
ENTRADAS_DERIVADAS = frozenset({"cohort_scan.json", "series_cohorts.json"})

#: Runner de `experiments/` que corre una receta que no declara `params.runner` (los lanes de E3 lo
#: ejecutan por subprocess, así que el sello de entradas tiene que enumerarlo desde aquí, no adivinarlo).
DEFAULT_RUNNER = "run_global_deep.py"


@dataclass(frozen=True)
class Receta:
    """Una receta declarada. ``primary`` decide si cuenta como respuesta o como contexto."""

    name: str
    role: str  # "primary" | "control"
    model: str
    params: dict
    space: str  # "diff" | "levels"
    notes: str

    @property
    def primary(self) -> bool:
        return self.role == "primary"


@dataclass(frozen=True)
class LaneSpec:
    """Un lane de E3 tal como lo pre-registra la baraja (M74-F): receta × cohorte × tabla, con su semilla.

    Vive aquí, y no en el orquestador, porque lo leen DOS: `run_e3_campaign` para lanzarlo y
    `score_e3_campaign` para exigir que exista exactamente ese conjunto. Dos derivaciones del plan
    serían dos universos que parecen el mismo.
    """

    recipe: str
    role: str
    cohort: str
    table: str
    runner: str
    seed: int

    @property
    def receipt_name(self) -> str:
        return f"receipt_{self.recipe}_{self.table}_{self.cohort}.json"


@dataclass(frozen=True)
class Deck:
    version: str
    universe: str
    cohorts: tuple[str, ...]
    tables: tuple[str, ...]
    seeds: tuple[int, ...]
    device: str
    frozen: dict
    recipes: dict[str, Receta]
    inputs: dict
    max_primary_per_cohort: int

    def primarias(self) -> list[Receta]:
        return [r for r in self.recipes.values() if r.primary]

    def controles(self) -> list[Receta]:
        return [r for r in self.recipes.values() if not r.primary]

    def runners(self) -> list[str]:
        """Guiones de `experiments/` que los lanes ejecutan: el declarado por cada receta o `DEFAULT_RUNNER`."""
        return sorted({r.params.get("runner", DEFAULT_RUNNER) for r in self.recipes.values()})

    def semilla(self) -> int:
        """La semilla pre-registrada de los lanes. ★ M74-F · deuda 3.

        Ningún runner consumía `seeds`: los 36 lanes deep corrían con 1 porque ése era su defecto y
        los 6 GBM con 42 porque ése era el suyo. El plan no tiene dimensión de semilla (un lane por
        receta × cohorte × tabla), así que la baraja tiene que declarar exactamente una; si algún
        día declara varias, el plan cambia de forma y esto debe fallar antes de entrenar.
        """
        if len(self.seeds) != 1:
            raise ValueError(
                f"la baraja declara {len(self.seeds)} semillas {list(self.seeds)}; el plan de E3 admite una"
            )
        return self.seeds[0]

    def exigir_semilla(self, seed: int) -> int:
        """Fail-closed: un lane con receta sólo corre con la semilla que la baraja pre-registró."""
        if seed not in self.seeds:
            raise ValueError(f"semilla {seed} fuera de la baraja (declara {list(self.seeds)}): no es el protocolo")
        return seed

    def lanes(self) -> list[LaneSpec]:
        """El plan COMPLETO de E3. Orden determinista: rol (primarias primero), receta, cohorte, tabla."""
        semilla = self.semilla()
        plan: list[LaneSpec] = []
        for rol, recetas in (("primary", self.primarias()), ("control", self.controles())):
            for receta in sorted(recetas, key=lambda r: r.name):
                runner = receta.params.get("runner", DEFAULT_RUNNER)
                for cohorte in self.cohorts:
                    for tabla in self.tables:
                        plan.append(LaneSpec(receta.name, rol, cohorte, tabla, runner, semilla))
        return plan

    def contrastar_entradas(self, root: Path) -> dict[str, dict]:
        """Hash declarado frente a hash vivo de cada entrada, con su clase. ★ M74-F · deuda 7.

        ``congelada``: tiene que coincidir — si no, los lanes corren sobre algo distinto de lo
        pre-registrado. ``derivada_en_campana``: la reescribe la propia transacción antes de E3; se
        registra la pareja (declarado, vivo) sin exigir igualdad, que es lo que significa delimitarla.
        """
        out: dict[str, dict] = {}
        for clave, declarado in sorted(self.inputs.items()):
            if clave in {"commit", "deck_version"}:
                continue
            ruta = Path(root) / RUTAS_ENTRADAS.get(clave, clave)
            vivo = hashlib.sha256(ruta.read_bytes()).hexdigest() if ruta.is_file() else None
            out[clave] = {
                "kind": "derivada_en_campana" if clave in ENTRADAS_DERIVADAS else "congelada",
                "declared": declarado,
                "live": vivo,
                "status": "ausente" if vivo is None else ("igual" if vivo == declarado else "difiere"),
            }
        return out

    def receta(self, nombre: str) -> Receta:
        """La única puerta: una receta no declarada no existe para la campaña."""
        if nombre not in self.recipes:
            declaradas = ", ".join(sorted(self.recipes))
            raise KeyError(f"receta {nombre!r} no está en la baraja pre-registrada; hay: {declaradas}")
        return self.recipes[nombre]


def cargar_deck(ruta: Path = DECK_PATH) -> Deck:
    """Lee y VALIDA la baraja. Fail-closed: un archivo incoherente no deja entrenar."""
    crudo = json.loads(ruta.read_text())
    faltan = {
        "version",
        "universe",
        "cohorts",
        "tables",
        "seeds",
        "device",
        "frozen",
        "recipes",
        "inputs",
        "max_primary_per_cohort",
    } - set(crudo)
    if faltan:
        raise ValueError(f"la baraja no declara {sorted(faltan)}")
    if crudo["device"] != "cpu":
        raise ValueError(f"la campaña es de CPU; la baraja declara device={crudo['device']!r}")

    recetas: dict[str, Receta] = {}
    for nombre, r in crudo["recipes"].items():
        if r["role"] not in {"primary", "control"}:
            raise ValueError(f"{nombre}: role debe ser primary o control, no {r['role']!r}")
        if r["space"] not in {"diff", "levels"}:
            raise ValueError(f"{nombre}: space debe ser diff o levels, no {r['space']!r}")
        recetas[nombre] = Receta(
            name=nombre,
            role=r["role"],
            model=r["model"],
            params=dict(r["params"]),
            space=r["space"],
            notes=r.get("notes", ""),
        )

    tope = int(crudo["max_primary_per_cohort"])
    primarias = [r for r in recetas.values() if r.primary]
    if len(primarias) > tope:
        raise ValueError(f"{len(primarias)} recetas primarias por cohorte; el tope declarado es {tope}")
    if not primarias:
        raise ValueError("la baraja no declara ninguna receta primaria")

    return Deck(
        version=str(crudo["version"]),
        universe=str(crudo["universe"]),
        cohorts=tuple(crudo["cohorts"]),
        tables=tuple(crudo["tables"]),
        seeds=tuple(int(s) for s in crudo["seeds"]),
        device=str(crudo["device"]),
        frozen=dict(crudo["frozen"]),
        recipes=recetas,
        inputs=dict(crudo["inputs"]),
        max_primary_per_cohort=tope,
    )
