"""E2 · Barrido EXPLORATORIO de lo ya puntuado, por cohorte de estabilidad. Sin reentrenar.

La pregunta de la épica E es si segmentar el panel por estabilidad permite que algún modelo
**bata al piso naïve-1 de su propia cohorte**. E2 la responde barato: no entrena nada, solo
vuelve a leer los MASE de hold-out que la campaña ya produjo y los agrupa por la partición
congelada de E1 (``RULE v1.0.0``).

**Protocolo congelado antes de calcular** (ver también ``vp_model.exact_tests``):

* **Universo**: ``evaluable ∩ cohorte``, por clave NOMINAL ``(país, categoría, tabla)``. Se exige
  **igualdad de conjuntos** contra el catálogo de E1 y **cero duplicados** en las entradas; si
  algo no cuadra, el barrido **se detiene** en vez de calcular sobre un universo distinto.
* **Referencia**: el naïve-1 **de la propia celda**, pareado serie a serie. Nunca un piso global:
  el naïve-1 de una cohorte congelada y el de una con avance regular son números distintos, y
  compararse contra la media de los dos es compararse contra nadie.
* **Celda** = ``tabla × cohorte``. Las tablas no se mezclan (protocolo distinto: ``MIN_TRAIN`` 60
  para FAD y 36 para DFF) y las cohortes tampoco (son poblaciones distintas, que es el punto).
* **Familia de Holm** = los modelos contrastados **dentro de una celda**. La multiplicidad nace
  de probar muchos modelos sobre la misma población; celdas distintas no forman una familia.
* **Contraste** bilateral exacto, ceros descartados, empates por rangos medios, tamaño de efecto
  de Hodges-Lehmann con intervalo: todo en ``vp_model.exact_tests``, congelado allí.
* **Veredicto** (``verdict_exploratory``), decidido antes de ver un solo resultado:
  ``bate_naive1`` si ``p_holm < 0.05`` y el efecto es negativo (menos error que el naïve-1);
  ``peor_que_naive1`` si ``p_holm < 0.05`` y es positivo; ``sin_evidencia`` en otro caso; y
  ``sin_datos_suficientes`` cuando no hay pares bastantes para que la prueba pueda rechazar.
* **MCS-90 es COMPLEMENTARIO**: acompaña, no sustituye ni redefine el contraste primario, y su
  pertenencia no cambia ningún veredicto.

**Todo lo que sale de aquí es exploratorio y así queda escrito en el artefacto.** E2 no elige
modelos, no promueve nada, no toca los umbrales de E1 y no descarta celdas por su resultado: lo
que sale, sale.

Uso:  ante/bin/python experiments/scan_cohorts.py [--out RUTA]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import exact_tests
from vp_model.config import HOLDOUT, MIN_TRAIN
from vp_model.provenance import ruta_legible
from vp_model.stability import RULE_VERSION

ROOT = Path(__file__).resolve().parent.parent
COHORTS = ROOT / "reports" / "eval" / "series_cohorts.json"
OUT = ROOT / "reports" / "eval" / "cohort_scan.json"
CLAVE = ("country", "category", "table")
REFERENCIA = "naive1"
ALFA = 0.05
CI_LEVEL = 0.95
MCS_LEVEL = 0.90
MCS_B = 9999
MCS_SEED = 20260909
SIGNIFICATIVOS = 12  # dígitos SIGNIFICATIVOS, no decimales: ver ``_num``

#: Los cuatro cuadernos de puntuación de la campaña. Nominales: si falta uno, el barrido para.
SCORECARDS = (
    "model_comparison_FAD21.csv",
    "model_comparison_DFF21.csv",
    "model_comparison_EB_FAD21.csv",
    "model_comparison_EB_DFF21.csv",
)


def _num(x: float) -> float:
    """Redondeo a dígitos SIGNIFICATIVOS, no a decimales.

    Redondear a 6 decimales convertía en ``0.0`` tanto una p exacta de ``7.45e-09`` como un
    tamaño de efecto de ``1.16e-09``: el artefacto habría publicado «p = 0» y «efecto nulo»
    para un contraste que sí tiene esos valores. Con dígitos significativos, un número pequeño
    sigue siendo pequeño en vez de desaparecer.
    """
    return float(f"{x:.{SIGNIFICATIVOS}g}")


class UniversoIncoherente(RuntimeError):
    """El universo puntuado no reproduce el publicado por E1: no se calcula nada."""


def _sha256(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _cargar_scorecards(base: Path) -> pd.DataFrame:
    marcos = []
    for nombre in SCORECARDS:
        ruta = base / nombre
        if not ruta.exists():
            raise UniversoIncoherente(f"falta el cuaderno de puntuación {nombre}")
        marcos.append(pd.read_csv(ruta))
    d = pd.concat(marcos, ignore_index=True)
    faltan = {"model", "hold_mase", *CLAVE} - set(d.columns)
    if faltan:
        raise UniversoIncoherente(f"columnas ausentes en los cuadernos: {sorted(faltan)}")
    dup = d.duplicated(["model", *CLAVE])
    if dup.any():
        ejemplos = d.loc[dup, ["model", *CLAVE]].head(3).to_dict("records")
        raise UniversoIncoherente(f"{int(dup.sum())} filas duplicadas (modelo × serie): {ejemplos}")
    return d


def _universo(cohortes: dict, puntuadas: pd.DataFrame) -> dict[tuple[str, str, str], dict]:
    """``evaluable ∩ cohorte`` con igualdad de conjuntos y referencia presente. Fail-closed."""
    catalogo = {tuple(s[c] for c in CLAVE): s for s in cohortes["series"]}
    if len(catalogo) != len(cohortes["series"]):
        raise UniversoIncoherente("el catálogo de cohortes trae claves repetidas")
    declarado = int(cohortes["population"]["n_evaluable"])
    if len(catalogo) != declarado:
        raise UniversoIncoherente(f"el catálogo tiene {len(catalogo)} series y declara {declarado} evaluables")
    ref = puntuadas[(puntuadas.model == REFERENCIA) & puntuadas.hold_mase.notna()]
    con_ref = {tuple(getattr(r, c) for c in CLAVE) for r in ref.itertuples()}
    sin_ref = sorted(set(catalogo) - con_ref)
    if sin_ref:
        raise UniversoIncoherente(f"{len(sin_ref)} series de la cohorte no tienen {REFERENCIA} puntuado: {sin_ref[:5]}")
    return catalogo


def _celdas(catalogo: dict) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    celdas: dict[tuple[str, str], list] = {}
    for clave, fila in catalogo.items():
        celdas.setdefault((fila["table"], fila["cohort"]), []).append(clave)
    return {k: sorted(v) for k, v in sorted(celdas.items())}


def _mase_por_serie(puntuadas: pd.DataFrame) -> dict[tuple[str, str, str, str], float]:
    return {
        (r.model, r.country, r.category, r.table): float(r.hold_mase)
        for r in puntuadas.itertuples()
        if pd.notna(r.hold_mase)
    }


def _mcs90(perdidas: dict[str, np.ndarray], semilla: int) -> dict:
    """Conjunto de modelos con capacidad predictiva equivalente al 90 %. COMPLEMENTARIO.

    Eliminación secuencial con el estadístico de rango sobre las diferencias pareadas y nula por
    **permutación de signos** (la misma exchangeabilidad del contraste primario), con semilla y
    número de permutaciones fijos para que el resultado sea reproducible.
    """
    vivos = sorted(perdidas)
    rng = np.random.default_rng(semilla)
    eliminados = []
    while len(vivos) > 1:
        matriz = np.vstack([perdidas[m] for m in vivos])
        dif = matriz[:, None, :] - matriz[None, :, :]  # (i, j, serie)
        medias = dif.mean(axis=2)
        desv = dif.std(axis=2, ddof=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.where(desv > 0, np.abs(medias) / (desv / np.sqrt(matriz.shape[1])), 0.0)
        t_obs = float(np.max(t))
        signos = rng.choice([-1.0, 1.0], size=(MCS_B, matriz.shape[1]))
        nulos = np.empty(MCS_B)
        for b in range(MCS_B):
            db = dif * signos[b]
            mb, sb = db.mean(axis=2), db.std(axis=2, ddof=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                tb = np.where(sb > 0, np.abs(mb) / (sb / np.sqrt(matriz.shape[1])), 0.0)
            nulos[b] = np.max(tb)
        p = float((1 + int((nulos >= t_obs).sum())) / (MCS_B + 1))
        if p > 1 - MCS_LEVEL:
            break
        peor = vivos[int(np.argmax(medias.mean(axis=1)))]
        eliminados.append(peor)
        vivos = [m for m in vivos if m != peor]
    return {
        "level": MCS_LEVEL,
        "members": sorted(vivos),
        "eliminated": eliminados,
        "permutations": MCS_B,
        "seed": MCS_SEED,
        "note": "complementario: no sustituye ni modifica el contraste primario",
    }


def scan(base: Path | None = None, cohorts_path: Path = COHORTS) -> dict:
    base = base or (ROOT / "reports" / "eval")
    cohortes = json.loads(cohorts_path.read_text())
    puntuadas = _cargar_scorecards(base)
    catalogo = _universo(cohortes, puntuadas)
    mase = _mase_por_serie(puntuadas)
    modelos = sorted({m for (m, *_rest) in mase} - {REFERENCIA})

    celdas_salida = []
    for (tabla, cohorte), series in _celdas(catalogo).items():
        ref = np.array([mase[(REFERENCIA, *s)] for s in series], dtype="float64")
        crudos: dict[str, dict] = {}
        perdidas = {REFERENCIA: ref}
        for modelo in modelos:
            presentes = [s for s in series if (modelo, *s) in mase]
            if len(presentes) == len(series):
                perdidas[modelo] = np.array([mase[(modelo, *s)] for s in series], dtype="float64")
            d = np.array([mase[(modelo, *s)] - mase[(REFERENCIA, *s)] for s in presentes], dtype="float64")
            registro: dict = {
                "model": modelo,
                "n_series_cell": len(series),
                "n_pairs": len(d),
                "n_missing": len(series) - len(presentes),
            }
            if len(d) < exact_tests.MIN_PAIRS or np.all(d == 0.0):
                registro["verdict_exploratory"] = "sin_datos_suficientes"
                registro["reason"] = (
                    "pares insuficientes para que un contraste bilateral exacto pueda rechazar"
                    if len(d) < exact_tests.MIN_PAIRS
                    else "todas las diferencias con la referencia son cero"
                )
            else:
                r = exact_tests.signed_rank_exact(d, ci_level=CI_LEVEL)
                registro.update(r.as_dict())
                registro["mean_mase"] = float(np.mean([mase[(modelo, *s)] for s in presentes]))
                # Descriptor SIN umbral: el efecto en unidades del propio nivel de la celda. Un
                # contraste de rangos con signo detecta el SIGNO de las diferencias, así que puede
                # ser significativo con diferencias de 1e-9; esta razón lo deja a la vista sin
                # inventar un umbral de materialidad ni tocar el veredicto congelado.
                mediana_ref = float(np.median(ref))
                registro["hl_relative_to_reference"] = r.hodges_lehmann / mediana_ref if mediana_ref else float("nan")
            crudos[modelo] = registro

        familia = {m: reg["p_value"] for m, reg in crudos.items() if "p_value" in reg}
        ajustadas = exact_tests.holm_adjust(familia)
        for m, reg in crudos.items():
            if m in ajustadas:
                reg["p_holm"] = ajustadas[m]
                if reg["p_holm"] < ALFA and reg["hodges_lehmann"] != 0.0:
                    reg["verdict_exploratory"] = "bate_naive1" if reg["hodges_lehmann"] < 0 else "peor_que_naive1"
                else:
                    # Un efecto EXACTAMENTE nulo no cae en ninguna de las dos ramas congeladas;
                    # se completa por el lado conservador, que solo puede quitar una afirmación.
                    reg["verdict_exploratory"] = "sin_evidencia"

        filas = [
            {k: (_num(v) if isinstance(v, float) else v) for k, v in sorted(reg.items())}
            for _, reg in sorted(crudos.items())
        ]
        celdas_salida.append(
            {
                "table": tabla,
                "cohort": cohorte,
                "n_series": len(series),
                "series": [dict(zip(CLAVE, s, strict=True)) for s in series],
                "reference": REFERENCIA,
                "reference_mean_mase": _num(float(np.mean(ref))),
                "reference_median_mase": _num(float(np.median(ref))),
                "holm_family_size": len(familia),
                "min_train": MIN_TRAIN[tabla],
                "models": filas,
                # El MCS solo corre donde el contraste primario pudo correr: si la celda no da
                # para concluir, un conjunto de confianza tampoco puede llenar ese hueco.
                "mcs90": (
                    _mcs90(perdidas, MCS_SEED) if len(perdidas) > 1 and len(series) >= exact_tests.MIN_PAIRS else None
                ),
            }
        )

    return {
        "status": "exploratorio",
        "scope": (
            "mide lo YA puntuado por la campaña; no entrena, no promueve, no elige modelos y no "
            "modifica la regla de cohortes"
        ),
        "rule_version": RULE_VERSION,
        "protocol": {
            "reference": REFERENCIA,
            "reference_scope": "naive-1 de la PROPIA celda, pareado serie a serie",
            "cell": "tabla × cohorte",
            "holm_family": "los modelos contrastados dentro de una celda",
            "test": "wilcoxon signed-rank bilateral exacto (permutación de signos condicional)",
            "zeros": "descartados; n_eff cuenta las diferencias no nulas",
            "ties": "rangos medios; el intervalo se marca aproximado si hay empates",
            "effect_size": "Hodges-Lehmann con intervalo libre de distribución",
            "alpha": ALFA,
            "ci_level": CI_LEVEL,
            "min_pairs": exact_tests.MIN_PAIRS,
            "verdict_rule": (
                "bate_naive1 si p_holm < alpha y el efecto es negativo; peor_que_naive1 si "
                "p_holm < alpha y es positivo; sin_evidencia en otro caso"
            ),
            "mcs": "MCS-90 complementario; no sustituye ni modifica el contraste primario",
            "holdout_months": HOLDOUT,
        },
        "population": {
            "n_evaluable": len(catalogo),
            "n_models_tested": len(modelos),
            "cells": len(celdas_salida),
            "eligibility": "evaluable ∩ cohorte, por clave nominal (país, categoría, tabla)",
        },
        "provenance": {
            "cohorts": ruta_legible(cohorts_path),
            "cohorts_sha256": _sha256(cohorts_path),
            "scorecards": {n: _sha256(base / n) for n in SCORECARDS},
        },
        "cells": celdas_salida,
    }


def write(informe: dict, destino: Path = OUT) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(informe, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return destino


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    informe = scan()
    ruta = write(informe, args.out)
    print(
        f"E2 EXPLORATORIO · RULE v{informe['rule_version']} · "
        f"{informe['population']['n_evaluable']} series · {informe['population']['cells']} celdas · "
        f"{informe['population']['n_models_tested']} modelos contra {REFERENCIA}"
    )
    for celda in informe["cells"]:
        cuenta: dict[str, int] = {}
        for m in celda["models"]:
            v = m["verdict_exploratory"]
            cuenta[v] = cuenta.get(v, 0) + 1
        print(
            f"  {celda['table']:>3} · {celda['cohort']:<10} n={celda['n_series']:>2} · "
            f"naive1 medio {celda['reference_mean_mase']:.3f} · {dict(sorted(cuenta.items()))}"
        )
    print(f"  → {ruta.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
