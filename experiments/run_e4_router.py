"""E4 · ¿Enrutar por cohorte de estabilidad bate al naïve-1 de CADA cohorte a 3, 6 y 12 meses?

El router elige, por cohorte y horizonte, el candidato con menor MASE medio en la ventana de
**selección** (objetivos anteriores al hold-out) y se evalúa **solo** sobre el hold-out: nunca ve
el dato con el que se le juzga. Los candidatos son los cinco clásicos congelados en julio
(``config.HORIZON_CANDIDATES``), así que el router **no puede** escoger la receta ganadora de E3
ni ``control-bitcn``.

El gate es el canónico y estaba congelado antes: efecto material **≥ 0.005**, Wilcoxon pareado
**bilateral**, **Holm** al 0.05 sobre la familia de los tres horizontes de una misma tabla ×
cohorte, y **cero** horizontes con pérdida material. Todo se lee por ``champion.load_deck()``.

**El resultado negativo también cierra E4**, y pase lo que pase el router **no se promueve ni se
despliega**: eso exige sombra prospectiva y autorización aparte.

Uso:  ante/bin/python experiments/run_e4_router.py [--out RUTA]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
from scipy.stats import wilcoxon

from vp_model import champion, horizon, significance
from vp_model.config import HORIZON_CANDIDATES
from vp_model.provenance import ruta_legible

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "reports" / "eval" / "e4_router_pool.json"
OUT = ROOT / "reports" / "eval" / "e4_router.json"
REFERENCIA = "naive1"
SIGNIFICATIVOS = 12


def _num(x: float) -> float:
    return float(f"{x:.{SIGNIFICATIVOS}g}")


def veredicto_de_celda(filas: list[dict], margen: float, permitida: float) -> tuple[str, list[int]]:
    """Regla de victoria congelada, aislada para poder probarla con entradas sintéticas.

    Gana solo si **en los tres horizontes** hay ganancia material y Holm rechaza, y si el número
    de horizontes con pérdida material no supera lo permitido (que es cero).
    """
    perdidas = [f["h"] for f in filas if f["effect"] <= -margen]
    gana = bool(filas) and all(f["effect"] >= margen and f["holm_reject"] for f in filas)
    if gana and len(perdidas) <= permitida:
        return "gana_al_naive1", perdidas
    return ("pierde_material" if perdidas else "sin_evidencia"), perdidas


class PoolAlterado(RuntimeError):
    """El pool persistido no es el que declara el catálogo: no se evalúa nada."""


def cargar_pool(ruta: Path = POOL) -> dict:
    """Pool exacto de E4, verificado por **igualdad de conjuntos** contra el catálogo canónico."""
    from vp_model import dataset

    doc = json.loads(ruta.read_text())
    persistido = {(s["country"], s["category"], s["table"]) for s in doc["series"]}
    if len(persistido) != len(doc["series"]):
        raise PoolAlterado("el pool persistido trae claves repetidas")
    if len(persistido) != doc["n"]:
        raise PoolAlterado(f"el pool declara {doc['n']} y trae {len(persistido)}")
    canonico = {(r.country, r.category, r.table) for r in dataset.evaluable_series().itertuples()}
    if persistido != canonico:
        falta, sobra = sorted(canonico - persistido)[:3], sorted(persistido - canonico)[:3]
        raise PoolAlterado(f"el pool no coincide con el catálogo (falta {falta}, sobra {sobra})")
    return doc


def evaluar(pool_path: Path = POOL) -> dict:
    deck = champion.load_deck()
    router_deck = deck["router"]
    horizontes = tuple(router_deck["horizons"])
    candidatos = tuple(router_deck["candidates"])
    if candidatos != tuple(HORIZON_CANDIDATES):
        raise ValueError("la baraja y el código discrepan en los candidatos del router")
    margen = float(deck["gate"]["material_margin"])
    alfa = float(deck["gate"]["holm_alpha"])
    permitida = float(router_deck["allowed_material_loss"])

    pool = cargar_pool(pool_path)
    celdas = []
    for tabla in sorted({s["table"] for s in pool["series"]}):
        miembros = [s for s in pool["series"] if s["table"] == tabla]
        cohortes = {(s["country"], s["category"]): s["cohort"] for s in miembros}
        series = sorted(cohortes)
        grid = horizon.mase_grid(tabla, series, candidatos, max(horizontes))

        por_cohorte: dict[str, list[dict]] = {}
        recetas: dict[str, dict] = {}
        for h in horizontes:
            receta = horizon.fit_router(tabla, h, cohortes, grid, candidatos)
            recetas[str(h)] = dict(sorted(receta.by_cohort.items()))
            router = horizon.router_series_mase(receta, cohortes, grid)
            for cohorte in sorted(set(cohortes.values())):
                claves = [k for k, c in cohortes.items() if c == cohorte]
                ref = pd.Series(
                    {
                        k: grid[(REFERENCIA, *k)]["holdout"][h]
                        for k in claves
                        if (REFERENCIA, *k) in grid and h in grid[(REFERENCIA, *k)]["holdout"]
                    },
                    dtype="float64",
                )
                comunes = router.index.intersection(ref.index)
                a, b = ref.loc[comunes], router.loc[comunes]
                efecto = float(a.mean() - b.mean())  # >0 = el router mejora
                if len(comunes) == 0 or (a - b).abs().sum() == 0:
                    p = p_uni = 1.0
                else:
                    p = float(wilcoxon(a.to_numpy(), b.to_numpy()).pvalue)
                    # SENSIBILIDAD, nunca gate: se registra para que se vea cuánto de la
                    # conclusión depende de exigir bilateralidad, no para relajar la exigencia.
                    p_uni = float(wilcoxon(a.to_numpy(), b.to_numpy(), alternative="greater").pvalue)
                por_cohorte.setdefault(cohorte, []).append(
                    {
                        "h": h,
                        "model": receta.by_cohort.get(cohorte),
                        "n_pairs": int(len(comunes)),
                        "router_mean_mase": _num(float(b.mean())) if len(b) else None,
                        "reference_mean_mase": _num(float(a.mean())) if len(a) else None,
                        "effect": _num(efecto),
                        "wilcoxon_p_two_sided": _num(p),
                        "sensitivity_one_sided_p": _num(p_uni),
                        "_sensitivity_note": "unilateral SOLO como sensibilidad; el gate es el bilateral",
                        "material_gain": bool(efecto >= margen),
                        "material_loss": bool(efecto <= -margen),
                    }
                )

        for cohorte, filas in sorted(por_cohorte.items()):
            adj = significance.holm({str(f["h"]): f["wilcoxon_p_two_sided"] for f in filas}, alpha=alfa)
            for f in filas:
                p_adj, reject = adj[str(f["h"])]
                f["holm_p"] = _num(float(p_adj))
                f["holm_reject"] = bool(reject)
            veredicto, perdidas = veredicto_de_celda(filas, margen, permitida)
            celdas.append(
                {
                    "table": tabla,
                    "cohort": cohorte,
                    "n_series": sum(1 for c in cohortes.values() if c == cohorte),
                    "reference": REFERENCIA,
                    "router_by_horizon": {str(f["h"]): f["model"] for f in filas},
                    "horizons": filas,
                    "material_loss_horizons": perdidas,
                    "verdict": veredicto,
                    "promotion": "NO — el gate retrospectivo no autoriza producción (sombra prospectiva aparte)",
                }
            )
        for h, elegidos in recetas.items():
            for celda in celdas:
                if celda["table"] == tabla:
                    celda.setdefault("selection_picks", {})[h] = elegidos

    resumen: dict[str, int] = {}
    for c in celdas:
        resumen[c["verdict"]] = resumen.get(c["verdict"], 0) + 1
    return {
        "status": "exploratorio",
        "scope": "gate RETROSPECTIVO por cohorte; no promueve, no despliega y no elige la receta después de ver el resultado",
        "deck_version": deck["version"],
        "rule_version": pool["rule_version"],
        "reference": REFERENCIA,
        "horizons": list(horizontes),
        "candidates": list(candidatos),
        "gate": {
            "material_margin": margen,
            "holm_alpha": alfa,
            "test": "wilcoxon pareado BILATERAL",
            "allowed_material_loss": permitida,
            "holm_family": "los horizontes {3,6,12} de una misma tabla × cohorte",
        },
        "selection": "MASE medio sobre objetivos ANTERIORES al hold-out; evaluación solo en hold-out",
        "pool": {"n": pool["n"], "sha256": hashlib.sha256(pool_path.read_bytes()).hexdigest()},
        "summary": dict(sorted(resumen.items())),
        "inputs": deck["inputs"],
        "cells": celdas,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    inf = evaluar()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(inf, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"E4 EXPLORATORIO · baraja v{inf['deck_version']} · {len(inf['cells'])} celdas · {inf['summary']}")
    for c in inf["cells"]:
        detalle = " ".join(
            f"h{f['h']}:{f['model']}({f['effect']:+.4f}{'*' if f['holm_reject'] else ''})" for f in c["horizons"]
        )
        print(f"  {c['table']:>3}/{c['cohort']:<11} n={c['n_series']:>2} · {detalle} → {c['verdict']}")
    print(f"  → {ruta_legible(args.out)}")


if __name__ == "__main__":
    main()
