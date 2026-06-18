"""Evalúa COMBINACIONES (ensembles/híbridos) y las loguea a MLflow vía tracking.

Dos familias:
  1. Combinaciones del pool local (``vp_model.ensemble``): curada (mediana de fuertes),
     media/mediana del set curado, selección-por-serie — código ya validado.
  2. Deep + parsimonia: la pregunta científica del EMPATE en FAD — ¿combinar el ganador
     profundo global (BiTCN/AutoBiTCN, de los CSV de la campaña) con ETS/Theta/SARIMA bate
     a cualquiera por separado? Mediana por serie×fecha, evaluada F-only con la escala única.

Corre en ``ante``. Uso:  ante/bin/python run_ensembles.py --mlflow
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import tracking
from vp_model import dataset, ensemble
from vp_model.metrics import naive_scale_before

REPORTS = Path(__file__).resolve().parent / "reports"


def _log(track: bool, table: str, name: str, mase: float, detail: str) -> None:
    print(f"  {table} {name:<38} MASE {mase:.4f}  ({detail})")
    if track:
        tracking.log_run(
            f"ensembles_{table}",
            f"{name}/{table}",
            params={"strategy": name, "table": table, "layer": "ensemble"},
            metrics={"hold_mase": mase},
            tags={"layer": "ensemble", "detail": detail},
        )


def pool_combinations(table: str, track: bool) -> None:
    """Combinaciones del pool local (curada + media/mediana del set curado)."""
    try:
        s = ensemble.curated_combination(table)
        _log(track, table, s.name, s.hold_mase, s.detail)
    except Exception as e:  # noqa: BLE001
        print(f"  curated {table} FALLO: {type(e).__name__}: {str(e)[:60]}")
    for s in ensemble.combinations(table):
        _log(track, table, s.name, s.hold_mase, s.detail)


def deep_plus_parsimony(table: str, deep_glob: str, deep_col: str, stat_models, track: bool) -> None:
    """Mediana(deep + estadísticos) por serie×fecha, evaluada F-only. Responde el empate FAD.

    ``deep_glob``: patrón de los CSV de forecast profundo (p.ej. 'global_FAD_camp_diff_s1.csv').
    ``deep_col``: columna del modelo profundo. ``stat_models``: lista de modelos del pool.
    """
    hf = REPORTS / f"holdout_forecasts_{table}.csv"
    deep_csv = REPORTS / deep_glob
    if not hf.exists() or not deep_csv.exists():
        print(f"  deep+parsimony {table}: faltan CSV ({hf.name} / {deep_glob}) — omitido")
        return
    stat = pd.read_csv(hf, parse_dates=["date"])
    stat = stat[stat.model.isin(stat_models)][["country", "category", "date", "model", "forecast", "actual"]]
    deep = pd.read_csv(deep_csv, parse_dates=["ds"])
    deep = deep[deep[deep_col].notna()].copy()
    deep["country"] = deep.unique_id.str.split("/").str[0]
    deep["category"] = deep.unique_id.str.split("/").str[2]
    deep = deep.rename(columns={"ds": "date", deep_col: "forecast"})
    deep["model"] = f"deep:{deep_col}"
    allf = pd.concat(
        [
            stat[["country", "category", "date", "model", "forecast"]],
            deep[["country", "category", "date", "model", "forecast"]],
        ],
        ignore_index=True,
    )
    comb = allf.groupby(["country", "category", "date"])["forecast"].median().reset_index()
    mases = []
    for (country, category), g in comb.groupby(["country", "category"]):
        try:
            full = dataset.load_series(country, category, table).astype("float64")
        except KeyError:
            continue
        g = g.sort_values("date")
        y = full.reindex(g["date"]).to_numpy()  # F-only: NaN donde no hay fecha real
        mask = ~np.isnan(y)
        if mask.sum() == 0:
            continue
        scale = naive_scale_before(full, g["date"].min())
        mae = float(np.mean(np.abs(y[mask] - g["forecast"].to_numpy()[mask])))
        mases.append(mae / scale)
    if mases:
        _log(
            track,
            table,
            f"mediana({deep_col}+{'+'.join(stat_models)})",
            float(np.mean(mases)),
            f"deep+parsimonia, {len(mases)} series",
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    for table in ("FAD", "DFF"):
        print(f"\n=== ENSEMBLES {table} ===")
        pool_combinations(table, args.mlflow)
        # deep+parsimonia: usa el primer CSV de la campaña que exista (semilla 1, diff)
        for glob, col in [
            (f"global_{table}_camp_diff_s1.csv", "BiTCN"),
            (f"global_{table}_camp_auto_s1.csv", "AutoBiTCN"),
        ]:
            deep_plus_parsimony(table, glob, col, ["theta", "ets", "sarima"], args.mlflow)


if __name__ == "__main__":
    main()
