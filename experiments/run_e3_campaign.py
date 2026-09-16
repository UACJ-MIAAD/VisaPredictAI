"""Orquesta los lanes de la campaña de cohortes E3, **derivados del deck congelado**.

Hasta M74-B los 42 lanes de E3 se lanzaban a mano. Eso bastaba para una campaña exploratoria
puntual, pero no para re-derivarla dentro de la transacción causal (#58, opción A): sin un
entrypoint, la cadena E no podía entrar al runbook ni al sello.

**Nada aquí se inventa.** El plan sale de ``docs/cohort_deck.json`` a través de la única puerta
(`vp_model.deck`): las recetas, las cohortes, las tablas, las semillas y el dispositivo están
congelados ahí, y el intérprete de cada lane lo decide la propia receta —el control GBM declara su
``runner`` y corre en el venv del producto—. Si el deck cambia, cambia el plan; si alguien añade
una receta, aparece su lane.

Uso:
    python experiments/run_e3_campaign.py --dry-run     # imprime el plan, no entrena
    python experiments/run_e3_campaign.py               # ejecuta los lanes en orden fijo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Intérprete por runner: el deep vive en `ante_nf`, el GBM en el venv del producto (lo dice el deck).
INTERPRETERS = {
    "run_global_deep.py": "ante_nf/bin/python",
    "run_global_gbm.py": "ante/bin/python",
}
#: Donde `score_e3_campaign.py` lee los recibos (`LANES`); el nombre repite el de los 42 lanes de M61.
RECEIPTS = Path("reports") / "campaign" / "e3"


@dataclass(frozen=True)
class Lane:
    """Un lane: una receta, una cohorte y una tabla. El orden de los campos fija el orden de corrida."""

    recipe: str
    role: str
    cohort: str
    table: str
    runner: str

    @property
    def command(self) -> list[str]:
        interprete = INTERPRETERS[self.runner]
        return [
            interprete,
            f"experiments/{self.runner}",
            "--recipe",
            self.recipe,
            "--cohort",
            self.cohort,
            "--table",
            self.table,
            # ★ R17 · sin `--receipt` ningún runner escribía su recibo y `score_e3_campaign` puntuaba los de la
            # campaña anterior sobre CSV nuevos. El nombre es el que los recibos de M61 ya usan.
            "--receipt",
            str(RECEIPTS / f"receipt_{self.recipe}_{self.table}_{self.cohort}.json"),
        ]


def plan() -> list[Lane]:
    """El plan COMPLETO, derivado del deck. Orden determinista: rol, receta, cohorte, tabla."""
    sys.path.insert(0, str(ROOT))
    from vp_model import deck as deck_mod

    d = deck_mod.cargar_deck()
    lanes: list[Lane] = []
    for rol, recetas in (("primary", d.primarias()), ("control", d.controles())):
        for receta in sorted(recetas, key=lambda r: r.name):
            runner = receta.params.get("runner", deck_mod.DEFAULT_RUNNER)  # R18: la misma autoridad que el sello
            if runner not in INTERPRETERS:
                raise SystemExit(f"receta {receta.name}: runner desconocido {runner!r}")
            for cohorte in d.cohorts:
                for tabla in d.tables:
                    lanes.append(Lane(receta.name, rol, cohorte, tabla, runner))
    return lanes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_e3_campaign", description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="imprime el plan en JSON y no entrena")
    args = ap.parse_args(argv)

    lanes = plan()
    if args.dry_run:
        print(json.dumps([{**vars(lane), "command": lane.command} for lane in lanes], ensure_ascii=False, indent=2))
        print(f"{len(lanes)} lanes derivados del deck", file=sys.stderr)
        return 0

    fallidos: list[str] = []
    for indice, lane in enumerate(lanes, 1):
        etiqueta = f"{lane.recipe}/{lane.cohort}/{lane.table}"
        print(f"##### lane {indice}/{len(lanes)} · {etiqueta}", flush=True)
        fin = subprocess.run(lane.command, cwd=ROOT, check=False)
        if fin.returncode != 0:
            # Un lane que falla NO se reintenta ni se esconde: queda en su receipt y aquí.
            print(f"##### LANE FALLIDO (exit {fin.returncode}): {etiqueta}", file=sys.stderr, flush=True)
            fallidos.append(etiqueta)
    if fallidos:
        print(f"✗ {len(fallidos)}/{len(lanes)} lanes fallaron: {', '.join(fallidos)}", file=sys.stderr)
        return 1
    print(f"✓ {len(lanes)} lanes completos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
