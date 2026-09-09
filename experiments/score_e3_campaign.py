"""E3 · Puntúa los lanes de la campaña por cohorte contra el naïve-1 de SU cohorte.

No entrena: lee lo que dejó cada lane, lo puntúa con el marcador canónico
(``metrics.mase_by_series``, máscara F-only y escala naïve de fuente única) y lo contrasta
contra el naïve-1 **de la misma cohorte**, pareado serie a serie, con el mismo aparato exacto
que E2 (``vp_model.exact_tests``). El naïve-1 sale de los cuadernos sellados que la baraja ya
registró por hash, así que E2 y E3 se comparan contra la misma referencia.

**Un lane que falló o no convergió también entra al informe**: su fila lo dice y no se sustituye
por otra receta. Todo el resultado queda etiquetado exploratorio.

Uso:  ante/bin/python experiments/score_e3_campaign.py [--out RUTA]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import exact_tests, metrics
from vp_model.deck import cargar_deck
from vp_model.provenance import ruta_legible

ROOT = Path(__file__).resolve().parent.parent
LANES = ROOT / "reports" / "campaign" / "e3"
SCORECARDS = (
    "model_comparison_FAD21.csv",
    "model_comparison_DFF21.csv",
    "model_comparison_EB_FAD21.csv",
    "model_comparison_EB_DFF21.csv",
)
OUT = ROOT / "reports" / "eval" / "e3_cohort_campaign.json"
REFERENCIA = "naive1"
SIGNIFICATIVOS = 12


def _num(x: float) -> float:
    return float(f"{x:.{SIGNIFICATIVOS}g}")


def naive1_por_serie() -> dict[tuple[str, str, str], float]:
    """MASE de hold-out del naïve-1, del mismo cuaderno sellado que usó E2."""
    marcos = [pd.read_csv(ROOT / "reports" / "eval" / n) for n in SCORECARDS]
    d = pd.concat(marcos, ignore_index=True)
    d = d[(d.model == REFERENCIA) & d.hold_mase.notna()]
    return {(r.country, r.category, r.table): float(r.hold_mase) for r in d.itertuples()}


def _mase_del_lane(csv: Path, columna: str, tabla: str) -> pd.Series:
    """MASE por serie de un lane, con el marcador CANÓNICO (F-only, escala única).

    Los lanes profundos dejan pronósticos (``unique_id``, ``ds``, columna del modelo) y hay que
    puntuarlos. El lane de control GBM ya escribe su MASE por serie con ESE MISMO marcador
    (``score_and_write`` llama a ``mase_by_series``), así que se lee tal cual en vez de volver a
    puntuar un marco que ni siquiera tiene la misma forma.
    """
    d = pd.read_csv(csv)
    if "hold_mase" in d.columns:
        return d.set_index(["country", "category"])["hold_mase"].astype("float64")
    partes = d["unique_id"].str.split("/", expand=True)
    marco = pd.DataFrame(
        {
            "country": partes[0],
            "category": partes[2],
            "date": pd.to_datetime(d["ds"]),
            "forecast": d[columna],
        }
    ).dropna(subset=["forecast"])
    return metrics.mase_by_series(marco, tabla)


def puntuar(directorio: Path = LANES) -> dict:
    deck = cargar_deck()
    ref = naive1_por_serie()
    cohortes = json.loads((ROOT / "reports" / "eval" / "series_cohorts.json").read_text())
    pertenencia = {(s["country"], s["category"], s["table"]): s["cohort"] for s in cohortes["series"]}

    lanes = []
    for receipt in sorted(directorio.glob("receipt_*.json")):
        r = json.loads(receipt.read_text())
        fila: dict = {
            "receipt": receipt.name,
            "recipe": r["recipe"],
            "recipe_role": r["recipe_role"],
            "recipe_model": r["recipe_model"],
            "recipe_space": r["recipe_space"],
            "table": r["table"],
            "cohort": r["cohort"],
            "status": r["status"],
            "seconds": r["timing"]["seconds"],
            "n_series": r["n_series"],
            "reference": REFERENCIA,
            "exception": r["exception"],
        }
        salidas = [ROOT / o["path"] for o in r["outputs"]]
        if r["status"] != "ok" or not salidas:
            fila["verdict_exploratory"] = "sin_resultado"
            lanes.append(fila)
            continue

        mase = _mase_del_lane(salidas[0], r["recipe"], r["table"])
        claves = [(p, c, r["table"]) for (p, c) in mase.index]
        pares = [
            (k, float(mase.loc[(k[0], k[1])]), ref[k])
            for k in claves
            if k in ref
            and np.isfinite(mase.loc[(k[0], k[1])])
            and (r["cohort"] == "all" or pertenencia.get(k) == r["cohort"])
        ]
        fila["n_scored"] = len(pares)
        if pares:
            fila["mase_by_series"] = True
            fila["mean_mase"] = _num(float(np.mean([m for _k, m, _n in pares])))
            fila["reference_mean_mase"] = _num(float(np.mean([n for _k, _m, n in pares])))
        d = np.array([m - n for _k, m, n in pares], dtype="float64")
        if len(d) < exact_tests.MIN_PAIRS or not len(d) or np.all(d == 0.0):
            fila["verdict_exploratory"] = "sin_datos_suficientes"
        else:
            res = exact_tests.signed_rank_exact(d)
            fila.update({k: _num(v) if isinstance(v, float) else v for k, v in res.as_dict().items()})
            fila["verdict_exploratory"] = (
                ("bate_naive1" if res.hodges_lehmann < 0 else "peor_que_naive1")
                if res.p_value < 0.05 and res.hodges_lehmann != 0.0
                else "sin_evidencia"
            )
        lanes.append(fila)

    # Holm por familia = las recetas PRIMARIAS de una misma celda (tabla x cohorte).
    for tabla in deck.tables:
        for cohorte in deck.cohorts:
            familia = {
                ln["receipt"]: ln["p_value"]
                for ln in lanes
                if ln["table"] == tabla
                and ln["cohort"] == cohorte
                and ln["recipe_role"] == "primary"
                and "p_value" in ln
            }
            for clave, p in exact_tests.holm_adjust(familia).items():
                for ln in lanes:
                    if ln["receipt"] == clave:
                        ln["p_holm"] = _num(p)
                        if ln["p_holm"] >= 0.05 or ln["hodges_lehmann"] == 0.0:
                            ln["verdict_exploratory"] = "sin_evidencia"

    primarios = [ln for ln in lanes if ln["recipe_role"] == "primary"]
    resumen: dict[str, int] = {}
    for ln in primarios:
        resumen[ln["verdict_exploratory"]] = resumen.get(ln["verdict_exploratory"], 0) + 1
    return {
        "status": "exploratorio",
        "scope": (
            "campaña de CPU local por cohorte; no promueve, no despliega y no elige recetas después de ver resultados"
        ),
        "deck_version": deck.version,
        "rule_version": cohortes["rule_version"],
        "device": "cpu",
        "reference": REFERENCIA,
        "reference_source": "cuadernos sellados de la campaña (los mismos de E2)",
        "holm_family": "las recetas primarias de una misma tabla x cohorte",
        "primary_summary": dict(sorted(resumen.items())),
        "n_lanes": len(lanes),
        "n_primary_lanes": len(primarios),
        "inputs": deck.inputs,
        "lanes": sorted(lanes, key=lambda x: (x["table"], x["cohort"], x["recipe"])),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    informe = puntuar()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(informe, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(
        f"E3 EXPLORATORIO · baraja v{informe['deck_version']} · {informe['n_lanes']} lanes "
        f"({informe['n_primary_lanes']} primarios) · primarios {informe['primary_summary']}"
    )
    print(f"  → {ruta_legible(args.out)}")


if __name__ == "__main__":
    main()
