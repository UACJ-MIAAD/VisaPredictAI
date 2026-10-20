"""Orquesta los lanes de la campaña de cohortes E3, **derivados del deck congelado**.

Hasta M74-B los 42 lanes de E3 se lanzaban a mano. Eso bastaba para una campaña exploratoria
puntual, pero no para re-derivarla dentro de la transacción causal (#58, opción A): sin un
entrypoint, la cadena E no podía entrar al runbook ni al sello.

**Nada aquí se inventa.** El plan sale de ``docs/cohort_deck.json`` a través de la única puerta
(`vp_model.deck`): las recetas, las cohortes, las tablas, las semillas y el dispositivo están
congelados ahí, y el intérprete de cada lane lo decide la propia receta —el control GBM declara su
``runner`` y corre en el venv del producto—. Si el deck cambia, cambia el plan; si alguien añade
una receta, aparece su lane.

★ M74-F · C4: con ``--complement`` los lanes corren bajo la transacción complementaria `running` de
este árbol (`vp_model.complement`): plan == baraja, recibos en SU directorio y ``CAMPAIGN_ID`` = el
id de la complementaria. Sin la bandera, un árbol con complementaria se niega a correr: la identidad
de un lane nunca se decide por accidente. Antes de CADA lane se re-comprueban las precondiciones del
contrato (código permitido, panel, entradas y DuckDB sellados), y los lanes heredan ``PYTHONPATH`` con
este árbol delante: sin él, el GBM importaría `vp_model` de la instalación editable, otro checkout.

Uso:
    python experiments/run_e3_campaign.py --dry-run     # imprime el plan, no entrena
    python experiments/run_e3_campaign.py               # ejecuta los lanes en orden fijo
    python experiments/run_e3_campaign.py --complement  # idem, dentro de la complementaria en curso
"""

from __future__ import annotations

import argparse
import json
import os
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
    seed: int
    #: Directorio de recibos: el de la campaña completa, o el que selló la complementaria.
    receipts_dir: str = str(RECEIPTS)

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
            # ★ M74-F · deuda 3: la semilla de la baraja, EXPLÍCITA. Sin ella cada runner usaba su
            # defecto (deep 1, GBM 42) y `seeds: [1]` no lo leía nadie.
            "--seed",
            str(self.seed),
            # ★ R17 · sin `--receipt` ningún runner escribía su recibo y `score_e3_campaign` puntuaba los de la
            # campaña anterior sobre CSV nuevos. El nombre es el que los recibos de M61 ya usan.
            "--receipt",
            str(Path(self.receipts_dir) / f"receipt_{self.recipe}_{self.table}_{self.cohort}.json"),
        ]


def plan(receipts_dir: str = str(RECEIPTS)) -> list[Lane]:
    """El plan COMPLETO, derivado del deck. Orden determinista: rol, receta, cohorte, tabla."""
    sys.path.insert(0, str(ROOT))
    from vp_model import deck as deck_mod

    lanes: list[Lane] = []
    # ★ M74-F: el plan sale de `Deck.lanes()`, la misma derivación que `score_e3_campaign` exige.
    for spec in deck_mod.cargar_deck(ROOT / "docs" / "cohort_deck.json").lanes():
        if spec.runner not in INTERPRETERS:  # R18: la misma autoridad que el sello
            raise SystemExit(f"receta {spec.recipe}: runner desconocido {spec.runner!r}")
        lanes.append(Lane(spec.recipe, spec.role, spec.cohort, spec.table, spec.runner, spec.seed, receipts_dir))
    return lanes


def duckdb_de_los_lanes() -> Path:
    """La DuckDB que abre un lane GBM: `vp_data.config.DUCKDB_PATH`, resuelta contra su cwd (ROOT)."""
    sys.path.insert(0, str(ROOT))
    from vp_data.config import DUCKDB_PATH

    return DUCKDB_PATH if DUCKDB_PATH.is_absolute() else ROOT / DUCKDB_PATH


def complementaria_en_curso() -> tuple[dict[str, str], str]:
    """``(identidad, directorio de recibos)`` de la complementaria de E3 `running`. Fail-closed."""
    sys.path.insert(0, str(ROOT))
    from vp_model import complement as cx
    from vp_model import deck as deck_mod

    try:
        doc = cx.precondiciones_calculo(ROOT, proposito="e3", duckdb_consumida=duckdb_de_los_lanes())
        p = cx.plan_de_baraja(doc, deck_mod.cargar_deck(ROOT / "docs" / "cohort_deck.json"))
        if p:
            raise cx.ComplementoError(p)
    except cx.ComplementoError as exc:
        raise SystemExit(f"✗ la complementaria no admite lanes: {exc}") from exc
    return cx.identidad(doc), str(doc["lanes_dir"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_e3_campaign", description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="imprime el plan en JSON y no entrena")
    ap.add_argument("--complement", action="store_true", help="corre dentro de la complementaria en curso")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    if args.complement:
        ident, receipts_dir = complementaria_en_curso()
        env.update(CAMPAIGN_ID=ident["campaign_id"], CAMPAIGN_SHA=ident["source_git_sha"])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH"))))
        lanes = plan(receipts_dir)
        print(f"complementaria {ident['campaign_id']} · recibos en {receipts_dir}", file=sys.stderr)
    elif (ROOT / "reports" / "campaign" / "complement.json").exists():
        raise SystemExit("✗ este árbol tiene una transacción complementaria: corre con --complement")
    else:
        lanes = plan()
    if args.dry_run:
        print(json.dumps([{**vars(lane), "command": lane.command} for lane in lanes], ensure_ascii=False, indent=2))
        print(f"{len(lanes)} lanes derivados del deck", file=sys.stderr)
        return 0

    fallidos: list[str] = []
    for indice, lane in enumerate(lanes, 1):
        etiqueta = f"{lane.recipe}/{lane.cohort}/{lane.table}"
        if args.complement:
            complementaria_en_curso()  # el código o la DuckDB pueden haber cambiado durante el lane anterior
        print(f"##### lane {indice}/{len(lanes)} · {etiqueta}", flush=True)
        fin = subprocess.run(lane.command, cwd=ROOT, check=False, env=env)
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
