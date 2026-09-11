"""Los dos análisis que el paper promete «con la re-derivación», y que nadie producía (H8).

`reports/paper_micai/paper.tex` afirma dos cosas en futuro:

1. «its effect on aggregate point error and coverage is quantified in the causal re-derivation,
   **comparing the same cohort before and after projection**» — el efecto de proyectar al cono de
   coherencia sobre el error puntual agregado y sobre la cobertura.
2. «this does not by itself establish superiority over an **out-of-sample seasonal-naïve forecast
   evaluated at the same origins** — a direct comparison planned with the re-derivation» — porque
   el MASE se escala con el naïve estacional **dentro** de muestra, que es otra cosa.

Ninguno existía en `experiments/`. Retirar las frases era el camino corto; el autor decidió
producir los análisis, porque son metodológicamente valiosos y ya están prometidos en público.

**Este módulo no promueve nada ni cambia el producto servido**: mide y escribe un artefacto.
Las funciones son puras sobre marcos, para poder probarlas sin correr una campaña.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
#: Los imports del producto viven DENTRO de las funciones: el módulo se ejecuta como guion y la
#: raíz sólo entra al path al invocarlo, así que importarlos arriba exigiría tres `noqa: E402`.

SCORECARD = ROOT / "reports" / "prospective" / "forecast_scorecard.csv"
WEB_FORECASTS = ROOT / "reports" / "prospective" / "web_forecasts.csv"
WEB_PRECONE = ROOT / "reports" / "prospective" / "web_forecasts_precone.csv"
OUT = ROOT / "reports" / "eval" / "paper_promises.json"
CLAVE = ("country", "category", "table")


# ─────────────────────────────────────────────────── 1 · efecto de la proyección al cono
def _root_on_path() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _seasonal_period() -> int:
    _root_on_path()
    from vp_model.config import SEASONAL_PERIOD

    return int(SEASONAL_PERIOD)


def cone_effect(forecasts: pd.DataFrame, actuals: pd.DataFrame) -> dict[str, Any]:
    """Error puntual agregado y cobertura, ANTES y DESPUÉS de proyectar al cono.

    Se comparan las **mismas celdas** en ambos lados (ésa es la promesa: *the same cohort*), y se
    reporta además el subconjunto que la proyección **altera**, donde el efecto vive: sobre el
    agregado se diluye, y decir sólo el agregado escondería la magnitud real del cambio.
    """
    _root_on_path()
    from vp_model import cone

    proyectado, contadores = cone.project(forecasts.copy())
    unidos_pre = forecasts.merge(actuals, on=[*CLAVE, "date"], how="inner")
    unidos_post = proyectado.merge(actuals, on=[*CLAVE, "date"], how="inner")
    if unidos_pre.empty:
        raise SystemExit("cone_effect: ninguna celda pronosticada tiene actual con el que compararse")

    alteradas = _altered_rows(forecasts, proyectado)
    return {
        "cells_compared": int(len(unidos_pre)),
        "cells_altered_by_projection": int(len(alteradas)),
        "violations": contadores,
        "aggregate": {
            "before": _point_and_coverage(unidos_pre),
            "after": _point_and_coverage(unidos_post),
        },
        "altered_only": {
            "before": _point_and_coverage(unidos_pre[unidos_pre.index.isin(alteradas)]),
            "after": _point_and_coverage(unidos_post[unidos_post.index.isin(alteradas)]),
        },
    }


def _altered_rows(antes: pd.DataFrame, despues: pd.DataFrame) -> pd.Index:
    """Índice de las filas cuyo valor puntual cambió al proyectar."""
    comun = antes.index.intersection(despues.index)
    distinto = ~np.isclose(
        antes.loc[comun, "days"].to_numpy(dtype="float64"),
        despues.loc[comun, "days"].to_numpy(dtype="float64"),
        equal_nan=True,
    )
    return comun[distinto]


def _point_and_coverage(df: pd.DataFrame) -> dict[str, float | int | None]:
    """MAE en días y cobertura empírica de las bandas. `None` donde no hay con qué medir."""
    if df.empty:
        return {"n": 0, "mae_days": None, "coverage80": None, "coverage95": None}
    err = (df["days"] - df["actual"]).abs()
    dentro80 = ((df["actual"] >= df["lo80"]) & (df["actual"] <= df["hi80"])).mean()
    dentro95 = ((df["actual"] >= df["lo95"]) & (df["actual"] <= df["hi95"])).mean()
    return {
        "n": int(len(df)),
        "mae_days": round(float(err.mean()), 4),
        "coverage80": round(float(dentro80), 4),
        "coverage95": round(float(dentro95), 4),
    }


# ─────────────────────────────────── 2 · naïve estacional FUERA de muestra, mismos orígenes
def seasonal_naive_oos(scorecard: pd.DataFrame, panel: pd.DataFrame, m: int | None = None) -> dict[str, Any]:
    """Compara el modelo desplegado contra un naïve estacional **fuera de muestra**.

    El MASE del proyecto se escala con el error del naïve estacional **dentro** de muestra, que es
    una normalización, no un rival. Aquí el rival se construye de verdad: para cada origen `o` y
    horizonte `h`, la predicción es ``y(o + h - m)`` usando **sólo** observaciones con fecha ≤ `o`.
    Si ese mes no está disponible al origen, el par se descarta — no se imputa — y se cuenta.
    """
    m = _seasonal_period() if m is None else m
    if scorecard.empty:
        raise SystemExit("seasonal_naive_oos: el scorecard está vacío")
    observado = panel[panel["status"] == "F"].copy()
    observado["bulletin_date"] = pd.to_datetime(observado["bulletin_date"])
    indice: dict[tuple, pd.Series] = {
        clave: g.set_index("bulletin_date")["days_since_base"].sort_index()
        for clave, g in observado.groupby(list(CLAVE), sort=False)
    }

    filas = []
    sin_referencia = 0
    for r in scorecard.itertuples():
        serie = indice.get((r.country, r.category, r.table))
        if serie is None:
            sin_referencia += 1
            continue
        origen = pd.Timestamp(r.origin)
        objetivo = pd.Timestamp(r.target)
        referencia = objetivo - pd.DateOffset(months=m)
        disponible = serie[serie.index <= origen]
        if referencia not in disponible.index:
            sin_referencia += 1
            continue
        filas.append(
            {
                "h": int(r.h),
                "abs_err_model": abs(float(r.pred) - float(r.actual)),
                "abs_err_snaive": abs(float(disponible.loc[referencia]) - float(r.actual)),
            }
        )
    if not filas:
        raise SystemExit("seasonal_naive_oos: ningún par tuvo referencia estacional disponible al origen")

    comparados = pd.DataFrame(filas)
    por_h = (
        comparados.groupby("h")[["abs_err_model", "abs_err_snaive"]]
        .mean()
        .round(4)
        .rename(columns={"abs_err_model": "mae_model", "abs_err_snaive": "mae_seasonal_naive_oos"})
    )
    por_h["ratio"] = (por_h["mae_model"] / por_h["mae_seasonal_naive_oos"]).round(4)
    return {
        "seasonal_period": int(m),
        "pairs_compared": int(len(comparados)),
        "pairs_without_seasonal_reference": int(sin_referencia),
        "overall": {
            "mae_model": round(float(comparados["abs_err_model"].mean()), 4),
            "mae_seasonal_naive_oos": round(float(comparados["abs_err_snaive"].mean()), 4),
            "ratio": round(float(comparados["abs_err_model"].mean() / comparados["abs_err_snaive"].mean()), 4),
            "model_wins_share": round(float((comparados["abs_err_model"] < comparados["abs_err_snaive"]).mean()), 4),
        },
        "by_horizon": {str(h): fila.to_dict() for h, fila in por_h.iterrows()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="analyze_paper_promises", description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    _root_on_path()
    from vp_data.config import PANEL_PATH

    scorecard = pd.read_csv(SCORECARD)
    panel = pd.read_csv(PANEL_PATH)
    actuals = (
        panel[panel["status"] == "F"][[*CLAVE, "bulletin_date", "days_since_base"]]
        .rename(columns={"bulletin_date": "date", "days_since_base": "actual"})
        .copy()
    )

    # ⚠️ El efecto del cono sólo es medible sobre pares MADUROS, y el marco pre-proyección no se
    # persistía hasta M74-B: para las añadas anteriores el «antes» no existe y no se puede
    # reconstruir. Se declara en vez de fabricarlo; a partir de esta campaña, madura y se mide.
    if WEB_PRECONE.is_file():
        cono: dict[str, Any] = cone_effect(pd.read_csv(WEB_PRECONE), actuals)
    else:
        cono = {
            "status": "sin_antes_persistido",
            "reason": (
                "el marco pre-proyección no existía antes de M74-B; el efecto se mide sobre pares "
                "maduros a partir de esta campaña, no retroactivamente"
            ),
        }

    salida = {
        "schema_version": 1,
        "promise_cone_projection": cono,
        "promise_seasonal_naive_oos": seasonal_naive_oos(scorecard, panel),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(salida, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"✓ promesas del paper cuantificadas → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
