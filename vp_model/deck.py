"""E3 · La baraja pre-registrada de recetas: qué es primario, qué es control, qué está congelado.

Una campaña que puede elegir su receta después de ver el resultado no mide nada. Por eso la
baraja vive en ``docs/cohort_deck.json``, se **commitea antes de entrenar** y este módulo es la
única puerta para leerla: el corredor no acepta una receta que no esté declarada, y no hay forma
de ascender un control a primario sin cambiar el archivo pre-registrado (lo que deja rastro en
la historia del repositorio).

Sólo stdlib: el corredor profundo vive en un entorno aislado que no trae el resto del producto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DECK_PATH", "Deck", "Receta", "cargar_deck"]

DECK_PATH = Path(__file__).resolve().parent.parent / "docs" / "cohort_deck.json"


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
