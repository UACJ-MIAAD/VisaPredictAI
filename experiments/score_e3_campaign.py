"""E3 · Puntúa los lanes de la campaña por cohorte contra el naïve-1 de SU cohorte.

No entrena: lee lo que dejó cada lane, lo puntúa con el marcador canónico
(``metrics.mase_by_series``, máscara F-only y escala naïve de fuente única) y lo contrasta
contra el naïve-1 **de la misma cohorte**, pareado serie a serie, con el mismo aparato exacto
que E2 (``vp_model.exact_tests``). El naïve-1 sale de los cuadernos sellados que la baraja ya
registró por hash, así que E2 y E3 se comparan contra la misma referencia.

**Un lane que falló o no convergió también entra al informe**: su fila lo dice y no se sustituye
por otra receta. Todo el resultado queda etiquetado exploratorio.

★ M74-F · C4: con ``--complement`` puntúa dentro de una transacción complementaria
(`vp_model.complement`), sólo si el LECTOR de la complementaria la acredita entera (madre, panel,
código, importaciones y lanes) y su plan es exactamente el que deriva la baraja del árbol.

Uso:  ante/bin/python experiments/score_e3_campaign.py [--out RUTA]
      ante/bin/python experiments/score_e3_campaign.py --complement --parent-root RAIZ_MADRE [--out RUTA]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import exact_tests, metrics
from vp_model.deck import Deck, cargar_deck
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


def naive1_por_serie(root: Path = ROOT) -> dict[tuple[str, str, str], float]:
    """MASE de hold-out del naïve-1, del mismo cuaderno sellado que usó E2."""
    marcos = [pd.read_csv(root / "reports" / "eval" / n) for n in SCORECARDS]
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


class E3NoAcreditado(RuntimeError):
    """Los recibos de E3 no son exactamente los del plan de ESTA campaña."""


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def campana_activa(root: Path = ROOT) -> dict[str, str]:
    """Identidad de la campaña `running` (fail-closed): E3 sólo se puntúa dentro de la campaña que lo corrió."""
    sys.path.insert(0, str(root / "experiments"))
    from hpo_winner_receipt import WinnerReceiptError, campaign_identity

    try:
        return campaign_identity(root)
    except WinnerReceiptError as exc:
        raise E3NoAcreditado(f"sin campaña en curso no hay contra qué acreditar los recibos de E3: {exc}") from exc


def acreditar_recibos(directorio: Path, deck: Deck, campana: dict[str, str], root: Path = ROOT) -> list[Path]:
    """★ M74-F · deuda 2 — la acreditación vive en la LECTURA.

    Hasta aquí `puntuar` recorría `receipt_*.json` y puntuaba lo que hubiera: un recibo de otra
    corrida, uno sobrante o uno ausente no cambiaban nada (los 42 de la cuarta campaña real no traían
    identidad y pasaron). Ahora, antes de puntuar un solo lane, se exige:

    * el conjunto EXACTO de recibos que deriva `Deck.lanes()` — ni sobrantes ni ausentes;
    * que cada recibo diga la receta, el rol, la cohorte y la tabla de su lane, y la semilla de la baraja;
    * que lleve la identidad de la campaña en curso (``campaign_id``, ``source_git_sha``, ``panel_sha256``);
    * que sus salidas existan y tengan el sha256 que declaró.

    Devuelve los recibos en el orden del plan. Junta TODOS los problemas antes de fallar.
    """
    plan = {spec.receipt_name: spec for spec in deck.lanes()}
    presentes = {p.name: p for p in directorio.glob("receipt_*.json")}
    problemas = [f"falta el recibo del lane {n}" for n in sorted(set(plan) - set(presentes))]
    problemas += [f"recibo fuera del plan de la baraja: {n}" for n in sorted(set(presentes) - set(plan))]
    for nombre in sorted(set(plan) & set(presentes)):
        spec, r = plan[nombre], json.loads(presentes[nombre].read_text())
        for campo, esperado in (
            ("recipe", spec.recipe),
            ("recipe_role", spec.role),
            ("cohort", spec.cohort),
            ("table", spec.table),
            ("seed", spec.seed),
        ):
            if r.get(campo) != esperado:
                problemas.append(f"{nombre}: {campo}={r.get(campo)!r}, la baraja pre-registra {esperado!r}")
        if r.get("campaign") != campana:
            problemas.append(f"{nombre}: identidad {r.get('campaign')!r} no es la de la campaña en curso")
        for salida in r.get("outputs") or []:
            ruta = root / salida["path"]
            if not ruta.is_file():
                problemas.append(f"{nombre}: falta su salida {salida['path']}")
            elif _sha256(ruta) != salida.get("sha256"):
                problemas.append(f"{nombre}: {salida['path']} cambió desde que el lane la escribió")
    # ★ M74-F · deuda 7: una entrada CONGELADA que no coincide con la baraja significa que los lanes
    # corrieron sobre algo distinto de lo pre-registrado. Las derivadas en campaña se delimitan aparte.
    for clave, c in deck.contrastar_entradas(root).items():
        if c["kind"] == "congelada" and c["status"] != "igual":
            problemas.append(f"entrada congelada {clave}: {c['status']} (declarada {c['declared'][:12]}…)")
    if problemas:
        raise E3NoAcreditado(f"{len(problemas)} problema(s) en los recibos de E3:\n  " + "\n  ".join(problemas))
    return [presentes[n] for n in plan]


def complementaria_acreditada(root: Path, parent_root: Path, deck: Deck) -> tuple[dict[str, str], Path, dict]:
    """``(identidad, directorio de recibos, acta)`` de una complementaria de E3 que el lector acredita.

    Fail-closed: exige `computed`/`accredited` (el lector re-verifica todo sin creer el estado), plan
    igual a `deck.lanes()` y ninguna campaña completa en curso en el mismo árbol.
    """
    from vp_model import complement as cx

    try:
        doc = cx.leer(root)
        p = cx.plan_de_baraja(doc, deck) if doc["purpose"] == "e3" else [("proposito", f"{doc['purpose']!r}")]
        p += cx.campana_concurrente(root)
        if p:
            raise cx.ComplementoError(p)
        acta = cx.acreditar(root, parent_root=parent_root, acreditar_lanes=acreditar_recibos)
    except cx.ComplementoError as exc:
        raise E3NoAcreditado(f"la complementaria no se acredita: {exc}") from exc
    return acta["identity"], root / doc["lanes_dir"], {**acta, "complement_id": doc["complement_id"]}


def puntuar(
    directorio: Path = LANES,
    *,
    campana: dict[str, str] | None = None,
    root: Path = ROOT,
    deck: Deck | None = None,
    complemento: Path | None = None,
) -> dict:
    """Puntúa los lanes. ``complemento`` = raíz de la MADRE: puntúa dentro de la complementaria de ``root``."""
    deck = deck if deck is not None else cargar_deck(root / "docs" / "cohort_deck.json")
    acta = None
    if complemento is not None:
        campana, directorio, acta = complementaria_acreditada(root, complemento, deck)
    campana = campana if campana is not None else campana_activa(root)
    recibos = acreditar_recibos(directorio, deck, campana, root=root)
    ref = naive1_por_serie(root)
    cohortes = json.loads((root / "reports" / "eval" / "series_cohorts.json").read_text())
    pertenencia = {(s["country"], s["category"], s["table"]): s["cohort"] for s in cohortes["series"]}

    lanes = []
    for receipt in recibos:
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
        salidas = [root / o["path"] for o in r["outputs"]]
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
        "campaign": campana,
        "identity_source": "complement" if acta is not None else "campaign",
        **({"complement": acta} if acta is not None else {}),
        "rule_version": cohortes["rule_version"],
        "device": "cpu",
        "reference": REFERENCIA,
        "reference_source": "cuadernos sellados de la campaña (los mismos de E2)",
        "holm_family": "las recetas primarias de una misma tabla x cohorte",
        "primary_summary": dict(sorted(resumen.items())),
        "n_lanes": len(lanes),
        "n_primary_lanes": len(primarios),
        "inputs": deck.inputs,
        # M74-F · deuda 7: declarado frente a vivo; las derivadas en campaña quedan delimitadas aquí.
        "inputs_contrast": deck.contrastar_entradas(root),
        "lanes": sorted(lanes, key=lambda x: (x["table"], x["cohort"], x["recipe"])),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--complement", action="store_true", help="puntúa dentro de la complementaria de este árbol")
    ap.add_argument("--parent-root", type=Path, help="raíz de la campaña madre (obligatoria con --complement)")
    args = ap.parse_args(argv)
    if args.complement != (args.parent_root is not None):
        ap.error("--complement y --parent-root van juntos")
    informe = puntuar(complemento=args.parent_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(informe, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(
        f"E3 EXPLORATORIO · baraja v{informe['deck_version']} · {informe['n_lanes']} lanes "
        f"({informe['n_primary_lanes']} primarios) · primarios {informe['primary_summary']}"
    )
    print(f"  → {ruta_legible(args.out)}")


if __name__ == "__main__":
    main()
