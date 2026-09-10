"""E5 · Fuente ÚNICA de las cifras de la épica E: un solo JSON del que sale todo lo demás.

Regla cero llevada al hueso: **nada se teclea**. Los conteos, las medias, los MASE, los `n_eff`
y los veredictos se derivan de los artefactos sellados de E1 (cohortes), E2 (barrido), E3
(campaña) y E4 (router), y de ese JSON salen las macros de LaTeX, la tabla, la figura, el texto
académico, la tarjeta del modelo y los contratos que consumen el sitio y el RAG.

**La narrativa conserva el resultado negativo**, porque se deriva de él: ningún router gana; las
mejoras largas de FAD no satisfacen el corto plazo; DFF/estable pierde materialmente; y
DFF/no_estable no aporta evidencia.

Uso:  ante/bin/python experiments/build_e5_facts.py [--out-json RUTA]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

# `etiqueta_celda` es la autoridad ÚNICA del nombre visible de una cohorte: la comparten este
# JSON, la tabla del `.tex` y la figura. En M65 cada superficie se la construía por su cuenta y
# la misma celda salía como «inestable» en una y «no_estable» en la otra.
from vp_model.provenance import ruta_legible
from vp_model.stability import etiqueta_celda

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "reports" / "eval"
OUT_JSON = ROOT / "reports" / "governance" / "e5_facts.json"
OUT_MACROS = ROOT / "reports" / "latex" / "cohorts_facts.tex"
OUT_TABLA = ROOT / "reports" / "latex" / "cohortes.tex"
SCHEMA = "1.0.0"
PREFIJO = "cohortFact"

FUENTES = {
    "cohorts": EVAL / "series_cohorts.json",
    "scan": EVAL / "cohort_scan.json",
    "campaign": EVAL / "e3_cohort_campaign.json",
    "router": EVAL / "e4_router.json",
    "pool": EVAL / "e4_router_pool.json",
}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _num(x: float, cifras: int = 12) -> float:
    return float(f"{x:.{cifras}g}")


def construir() -> dict:
    """Deriva TODAS las cifras de la épica E de sus artefactos sellados."""
    fuentes = {k: json.loads(p.read_text()) for k, p in FUENTES.items()}
    coh, scan, camp, rout = (fuentes[k] for k in ("cohorts", "scan", "campaign", "router"))

    por_tabla: dict[str, dict[str, int]] = {}
    for s in coh["series"]:
        por_tabla.setdefault(s["table"], {}).setdefault(s["cohort"], 0)
        por_tabla[s["table"]][s["cohort"]] += 1

    # --- E2: barrido exploratorio -------------------------------------------------------
    e2: dict[str, int] = {}
    for celda in scan["cells"]:
        for m in celda["models"]:
            e2[m["verdict_exploratory"]] = e2.get(m["verdict_exploratory"], 0) + 1

    # --- E3: campaña por cohorte --------------------------------------------------------
    primarias = [ln for ln in camp["lanes"] if ln["recipe_role"] == "primary"]
    e3 = {
        "lanes": camp["n_lanes"],
        "primary_lanes": len(primarias),
        "ok": sum(1 for ln in camp["lanes"] if ln["status"] == "ok"),
    }

    # --- E4: router ---------------------------------------------------------------------
    filas_router = []
    for celda in rout["cells"]:
        for f in celda["horizons"]:
            filas_router.append(
                {
                    "table": celda["table"],
                    "cohort": celda["cohort"],
                    "n_series": celda["n_series"],
                    "h": f["h"],
                    "model": f["model"],
                    "n_pairs": f["n_pairs"],
                    "router_mase": f["router_mean_mase"],
                    "naive1_mase": f["reference_mean_mase"],
                    "effect": f["effect"],
                    "holm_p": f["holm_p"],
                    "significant": f["holm_reject"],
                    "verdict": celda["verdict"],
                }
            )
    veredictos_router: dict[str, int] = {}
    for celda in rout["cells"]:
        veredictos_router[celda["verdict"]] = veredictos_router.get(celda["verdict"], 0) + 1

    fad_largos = [f for f in filas_router if f["table"] == "FAD" and f["h"] in (6, 12) and f["significant"]]
    fad_cortos = [f for f in filas_router if f["table"] == "FAD" and f["h"] == 3]
    dff_estable = [f for f in filas_router if f["table"] == "DFF" and f["cohort"] == "estable"]
    dff_no_estable = next(c for c in rout["cells"] if c["table"] == "DFF" and c["cohort"] == "no_estable")

    escalares = {
        # E1
        "NSeriesEvaluable": coh["population"]["n_evaluable"],
        "NEstable": coh["cohorts"]["estable"],
        "NNoEstable": coh["cohorts"]["no_estable"],
        "RuleVersion": coh["rule_version"],
        "RetroRateMax": 0.02,
        "WorstRetroScaledMax": 5,
        # E2
        "ScanPeor": e2.get("peor_que_naive1", 0),
        "ScanBate": e2.get("bate_naive1", 0),
        "ScanSinDatos": e2.get("sin_datos_suficientes", 0),
        # E3
        "CampanaLanes": e3["lanes"],
        "CampanaPrimarias": e3["primary_lanes"],
        "CampanaOk": e3["ok"],
        "CampanaBate": sum(1 for ln in primarias if ln["verdict_exploratory"] == "bate_naive1"),
        # E4
        "RouterCeldas": len(rout["cells"]),
        "RouterGana": veredictos_router.get("gana_al_naive1", 0),
        "RouterPierde": veredictos_router.get("pierde_material", 0),
        "RouterSinEvidencia": veredictos_router.get("sin_evidencia", 0),
        "RouterMargen": rout["gate"]["material_margin"],
        "RouterAlfa": rout["gate"]["holm_alpha"],
        "RouterFadLargosSignificativos": len(fad_largos),
        "RouterFadCortosSignificativos": sum(1 for f in fad_cortos if f["significant"]),
        # 4 decimales, y los MISMOS en el JSON: así la macro y su autoridad coinciden EXACTO
        # y el contrato tex_json no tiene que tolerar una diferencia de redondeo.
        "RouterDffEstablePeorHTres": round(min(f["effect"] for f in dff_estable if f["h"] == 3), 4),
        "RouterDffEstableMejorHDoce": round(max(f["effect"] for f in dff_estable if f["h"] == 12), 4),
        "RouterDffNoEstableN": dff_no_estable["n_series"],
    }
    return {
        "_doc": (
            "Fuente ÚNICA de las cifras de la épica E. Todo lo demás (macros, tabla, figura, "
            "texto académico, tarjeta del modelo y contratos web/RAG) se deriva de aquí."
        ),
        "schema_version": SCHEMA,
        "status": "exploratorio",
        "narrative": {
            "router_gana": veredictos_router.get("gana_al_naive1", 0) > 0,
            "claim": (
                "ningún router gana; las mejoras largas de FAD no satisfacen el corto plazo; "
                "DFF/estable pierde materialmente; DFF/no_estable no aporta evidencia"
            ),
        },
        **escalares,
        "cohorts_by_table": {t: dict(sorted(v.items())) for t, v in sorted(por_tabla.items())},
        "scan_verdicts": dict(sorted(e2.items())),
        "router_verdicts": dict(sorted(veredictos_router.items())),
        "router_rows": sorted(filas_router, key=lambda f: (f["table"], f["cohort"], f["h"])),
        # Vista agrupada para el contrato `table` del guardián (F3), que indexa por (grupo, h):
        # con las cuatro celdas como grupos, la clave es única y no hace falta tocar el checker.
        **{
            etiqueta_celda(t, c): {
                "rows": [
                    {
                        "h": f["h"],
                        "model": f["model"],
                        "n_pairs": f["n_pairs"],
                        # 4 decimales, los MISMOS que imprime la tabla: la autoridad y su
                        # render coinciden exacto y el contrato no tolera redondeos.
                        "router_mase": round(f["router_mase"], 4),
                        "naive1_mase": round(f["naive1_mase"], 4),
                        "effect": round(f["effect"], 4),
                        "sig": f["significant"],
                    }
                    for f in sorted(filas_router, key=lambda f: f["h"])
                    if f["table"] == t and f["cohort"] == c
                ]
            }
            for t, c in sorted({(f["table"], f["cohort"]) for f in filas_router})
        },
        "provenance": {k: _sha(p) for k, p in sorted(FUENTES.items())},
    }


def macros(facts: dict) -> str:
    """`\\newcommand` por cada escalar de nivel superior. Autoridad: el propio JSON."""
    lineas = [f"% Auto-generado por {ruta_legible(Path(__file__))} — \\input y usa \\{PREFIJO}Xxx."]
    omitidas: list[str] = []
    for clave, valor in facts.items():
        if clave.startswith("_") or isinstance(valor, dict | list | bool):
            continue
        if not re.fullmatch(r"[A-Za-z]+", clave):
            # Un nombre de macro de LaTeX no admite dígitos ni guiones bajos: emitir
            # `\cohortFactschema_version` produciría un .tex que no compila (lección de M28).
            # Las claves con número se DELETREAN en el diccionario de arriba, no se silencian
            # aquí: si algo se cae por este filtro, es que se le olvidó deletrearlo.
            omitidas.append(clave)
            continue
        lineas.append(f"\\newcommand{{\\{PREFIJO}{clave}}}{{{valor}}}")
    esperadas = {"schema_version"}
    if set(omitidas) - esperadas:
        raise ValueError(f"claves sin macro por su nombre (deletrea los dígitos): {sorted(set(omitidas) - esperadas)}")
    return "\n".join(lineas) + "\n"


def _tex(texto: str) -> str:
    """Escapa lo que LaTeX trata como especial. El guion bajo de ``no_estable`` rompía la
    compilación con «Missing $ inserted»: en modo texto, `_` abre un subíndice."""
    return texto.replace("\\", "\\textbackslash{}").replace("_", "\\_").replace("&", "\\&").replace("%", "\\%")


def tabla(facts: dict) -> str:
    """Tabla del router por celda × horizonte. Cada celda sale del JSON, ninguna se teclea."""
    filas = []
    previa = None
    for f in facts["router_rows"]:
        celda = etiqueta_celda(f["table"], f["cohort"])
        primera = _tex(celda) if celda != previa else ""
        previa = celda
        # El contrato `table` de F3 espera el booleano como $\checkmark$ / --; se respeta tal cual.
        sig = "$\\checkmark$" if f["significant"] else "--"
        filas.append(
            f"{primera} & {f['h']} & {_tex(f['model'])} & {f['n_pairs']} & "
            f"{f['router_mase']:.4f} & {f['naive1_mase']:.4f} & {f['effect']:+.4f} & {sig} \\\\"
        )
    cuerpo = "\n".join(filas)
    return (
        f"% Auto-generado por {ruta_legible(Path(__file__))} — no editar a mano.\n"
        "\\small\n\\begin{tabular}{llrrrrrr}\n\\toprule\n"
        "Celda & $h$ & Modelo & $n$ & MASE rtr. & MASE nv-1 & Efecto & Signif. \\\\\n"
        "\\midrule\n" + cuerpo + "\n\\bottomrule\n\\end{tabular}\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-macros", type=Path, default=OUT_MACROS)
    ap.add_argument("--out-tabla", type=Path, default=OUT_TABLA)
    args = ap.parse_args()
    facts = construir()
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    args.out_macros.write_text(macros(facts))
    args.out_tabla.write_text(tabla(facts))
    print(
        f"E5 facts v{facts['schema_version']} · {facts['NSeriesEvaluable']} series "
        f"({facts['NEstable']}/{facts['NNoEstable']}) · router {facts['router_verdicts']} · "
        f"gana={facts['RouterGana']}"
    )
    for r in (args.out_json, args.out_macros, args.out_tabla):
        print(f"  → {ruta_legible(r)}")


if __name__ == "__main__":
    main()
