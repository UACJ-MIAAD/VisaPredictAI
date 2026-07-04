"""Evalúa COMBINACIONES (ensembles/híbridos) y las loguea a MLflow vía tracking.

Tres familias:
  1. Combinaciones del pool local (``vp_model.ensemble``): curada (mediana de fuertes),
     media/mediana del set curado, selección-por-serie — ahora puntuadas con el scorer
     canónico F-only y el denominador deduplicado (AM4b/AM4d).
  2. Best-K por serie (AM1): mediana de los K modelos con mejor ``sel_mase``
     (leakage-free, sin STRONG_SET retrospectivo) -> ``reports/eval/ensemble_bestk_{table}.csv``.
  3. Deep + parsimonia: ¿combinar el ganador profundo global (BiTCN/AutoBiTCN) con
     ETS/Theta/SARIMA bate a cualquiera por separado? AM4a: el deep se agrega por
     MEDIANA ENTRE SEMILLAS (todos los ``_s*`` de la campaña) ANTES de combinar — el
     código viejo montaba el veredicto sobre la semilla 1 solamente.

Corre en ``ante``. Uso:  ante/bin/python experiments/run_ensembles.py --mlflow
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from vp_data import tracking
from vp_model import ensemble
from vp_model.metrics import mase_by_series

REPORTS = Path(__file__).resolve().parent.parent / "reports"


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


def best_k_section(table: str, track: bool) -> None:
    """AM1: mediana-de-los-mejores-K por serie (K por sel_mase, leakage-free) + CSV."""
    try:
        report = ensemble.best_k_report(table)
    except FileNotFoundError as e:
        print(f"  best-K {table}: falta CSV ({e}) — omitido")
        return
    out = REPORTS / "eval" / f"ensemble_bestk_{table}.csv"
    report.to_csv(out, index=False)
    for r in report[report.country == "ALL"].itertuples():
        _log(track, table, f"best-{r.k} por serie (mediana)", float(r.hold_mase), f"K={r.k}, sel_mase, dedup")
    print(f"  -> {out.relative_to(REPORTS.parent)}")


def load_deep_median(paths: list[Path], deep_col: str) -> pd.DataFrame:
    """AM4a — median across SEEDS per (unique_id, ds) BEFORE any combination.

    The old code consumed a single ``_s1`` CSV, so the deep+parsimony verdict rode on one
    arbitrary seed. Returns an empty frame when no CSV carries ``deep_col`` (the caller
    reports the gap; the AQ campaign re-materializes the pools).
    """
    frames = []
    for p in paths:
        d = pd.read_csv(p, parse_dates=["ds"])
        if deep_col not in d.columns:
            continue
        frames.append(d[d[deep_col].notna()][["unique_id", "ds", deep_col]])
    if not frames:
        return pd.DataFrame(columns=["unique_id", "ds", deep_col])
    return pd.concat(frames, ignore_index=True).groupby(["unique_id", "ds"], as_index=False)[deep_col].median()


def deep_plus_parsimony(table: str, deep_glob: str, deep_col: str, stat_models: list[str], track: bool) -> None:
    """Mediana(deep + estadísticos) por serie×fecha, F-only y con denominador deduplicado.

    ``deep_glob``: glob de los CSV multi-semilla de la campaña (p. ej.
    ``global_FAD_camp_diff_s*.csv``); las semillas se agregan por mediana ANTES de
    combinar (AM4a). El MASE sale de ``metrics.mase_by_series`` (AM4d; los reales vienen
    del almacén, F-only) sobre representantes de pseudo-réplica (AM4b).
    """
    hf = REPORTS / "eval" / f"holdout_forecasts_{table}.csv"
    seed_paths = sorted((REPORTS / "campaign").glob(deep_glob))
    if not hf.exists() or not seed_paths:
        print(f"  deep+parsimony {table}: faltan CSV ({hf.name} / {deep_glob}) — omitido")
        return
    fc = pd.read_csv(hf, parse_dates=["date"])
    stat = fc[fc.model.isin(stat_models)][["country", "category", "date", "model", "forecast"]]
    deep = load_deep_median(seed_paths, deep_col)
    if deep.empty:
        print(f"  deep+parsimony {table}: ningún CSV de {deep_glob} trae {deep_col} — omitido")
        return
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
    comb, _n_raw, n_eff = ensemble.representative_filter(comb, table, fc)
    # AM4d: canonical scorer (actuals from the warehouse, F-only mask, shared naive scale).
    mases = mase_by_series(comb, table)
    if mases.count():
        _log(
            track,
            table,
            f"mediana({deep_col}+{'+'.join(stat_models)})",
            float(mases.mean()),
            f"deep mediana de {len(seed_paths)} semillas + parsimonia, {n_eff} series efectivas",
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlflow", action="store_true")
    args = ap.parse_args()
    for table in ("FAD", "DFF"):
        print(f"\n=== ENSEMBLES {table} ===")
        pool_combinations(table, args.mlflow)
        best_k_section(table, args.mlflow)
        # deep+parsimonia: TODAS las semillas de la campaña, agregadas por mediana (AM4a)
        for glob, col in [
            (f"global_{table}_camp_diff_s*.csv", "BiTCN"),
            (f"global_{table}_camp_auto_s*.csv", "AutoBiTCN"),
        ]:
            deep_plus_parsimony(table, glob, col, ["theta", "ets", "sarima"], args.mlflow)


if __name__ == "__main__":
    main()
