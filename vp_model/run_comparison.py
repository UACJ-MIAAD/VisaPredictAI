"""Comparación del catálogo de modelos (``config.MODEL_NAMES``) por walk-forward (US-F1, US-A3).

Corre el catálogo sobre un conjunto de series y consolida las métricas en
``reports/eval/model_comparison.csv`` (una fila por modelo×serie, con ``run_id`` para
trazar la corrida). Además registra la procedencia completa de cada corrida en el
ledger append-only ``reports/eval/experiment_runs.jsonl`` (run_id, commit git, semilla,
versiones, hiperparámetros) — reproducibilidad auditable.

Uso:
    python -m vp_model.run_comparison                       # MX FAD familias
    python -m vp_model.run_comparison --country india --table DFF --block family
    python -m vp_model.run_comparison --block employment --models arima sarima
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from vp_model import config, dataset, significance, walkforward

log = config.get_logger(__name__)

REPORTS = Path(__file__).resolve().parent.parent / "reports"
OUT = REPORTS / "eval" / "model_comparison.csv"
LEDGER = REPORTS / "eval" / "experiment_runs.jsonl"


def run(
    series: list[tuple[str, str, str]],
    model_names: tuple[str, ...],
    run_id: str,
    track: bool = False,
) -> pd.DataFrame:
    """Walk-forward de cada modelo sobre cada serie -> DataFrame de métricas.

    ``track=True`` loguea cada backtest a ``tracking`` (JSONL env-agnóstico) para MLflow.
    """
    tracking = _tracking() if track else None
    rows = []
    for country, category, table in series:
        for name in model_names:
            t0 = time.time()
            try:
                r = walkforward.backtest(name, country, category, table)
                row = {
                    "run_id": run_id,
                    "model": name,
                    "country": country,
                    "category": category,
                    "table": table,
                    "sel_mase": r.selection["mase"],
                    "sel_smape": r.selection["smape"],
                    "sel_mae": r.selection["mae"],
                    "sel_rmse": r.selection["rmse"],
                    "hold_mase": r.holdout["mase"],
                    "hold_smape": r.holdout["smape"],
                    "hold_mae": r.holdout["mae"],
                    "hold_rmse": r.holdout["rmse"],
                    # Probabilístico (PI conforme 95%): MSIS (M5) + interval score + cobertura.
                    "hold_msis": r.holdout.get("msis", float("nan")),
                    "hold_interval_score": r.holdout.get("interval_score", float("nan")),
                    "hold_coverage": r.holdout.get("coverage", float("nan")),
                    # AI2: MASE against the naive-1 (random walk) scale, in parallel.
                    "sel_mase1": r.selection.get("mase1", float("nan")),
                    "hold_mase1": r.holdout.get("mase1", float("nan")),
                    "secs": round(time.time() - t0, 1),
                }
                rows.append(row)
                if tracking is not None:
                    block = "employment" if category.startswith("EB") else "family"
                    tracking.log_run(
                        f"pool_local_{table}",
                        f"{name}/{country}/{category}/{table}",
                        params={
                            # AO1: campaign run_id — the join key across MLflow, the
                            # comparison CSV and the experiment ledger.
                            "run_id": run_id,
                            "model": name,
                            "country": country,
                            "category": category,
                            "table": table,
                            "block": block,
                        },
                        metrics={
                            k: row[k]
                            for k in ("sel_mase", "hold_mase", "hold_smape", "hold_mae", "hold_msis", "hold_coverage")
                        },
                        tags={"layer": "pool_local"},
                    )
                log.info(
                    "%s/%s/%s %-11s sel MASE=%.3f hold MASE=%.3f (%ss)",
                    country,
                    category,
                    table,
                    name,
                    r.selection["mase"],
                    r.holdout["mase"],
                    row["secs"],
                )
            except Exception as e:  # noqa: BLE001 — una serie/modelo que falle no debe abortar el barrido
                log.warning("%s/%s/%s %-11s FAIL %s: %s", country, category, table, name, type(e).__name__, str(e)[:80])
                # B6: fila NaN en vez de omisión — un modelo que revienta en las series
                # difíciles promediaba solo las fáciles (sesgo de supervivencia invisible).
                rows.append(
                    {
                        "run_id": run_id,
                        "model": name,
                        "country": country,
                        "category": category,
                        "table": table,
                        **{
                            k: float("nan")
                            for k in (
                                "sel_mase",
                                "sel_smape",
                                "sel_mae",
                                "sel_rmse",
                                "hold_mase",
                                "hold_smape",
                                "hold_mae",
                                "hold_rmse",
                                "hold_msis",
                                "hold_interval_score",
                                "hold_coverage",
                                "sel_mase1",
                                "hold_mase1",
                            )
                        },
                        "secs": round(time.time() - t0, 1),
                    }
                )
    return pd.DataFrame(rows)


def _tracking():
    """Importa el módulo raíz ``tracking`` (env-agnóstico) con guard de ruta."""
    import sys

    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from vp_data import tracking

    return tracking


def summary(df: pd.DataFrame) -> pd.DataFrame:
    """Ranking de modelos por MASE de selección promedio (menor es mejor).

    B2: colapsa las pseudo-réplicas del corte mundial a una representante antes de
    promediar (si no, el corte mundial pesa varias veces en la media) y anota el n.
    """
    if {"country", "category"} <= set(df.columns):
        df, n_raw, n_eff = significance.dedup_series(df, value="hold_mase")
        if n_eff < n_raw:
            log.info("dedup pseudo-réplicas: %d series -> %d efectivas", n_raw, n_eff)
    # B6: visibilizar fallos — la media de un modelo con NaN cubre menos series
    fails = df.groupby("model")["hold_mase"].apply(lambda s: int(s.isna().sum()))
    for m, k in fails[fails > 0].items():
        log.warning("modelo %s: %d serie(s) FALLIDA(s) — su media cubre menos series", m, k)
    return (
        df.groupby("model")[["sel_mase", "hold_mase", "sel_smape", "hold_smape"]]
        .mean()
        .sort_values("sel_mase")
        .round(3)
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Comparación walk-forward del catálogo de modelos.")
    p.add_argument(
        "--country",
        default="mexico",
        choices=[*config.PILOT_COUNTRIES, "all"],
        help="área de cargabilidad (o 'all' para las cinco piloto)",
    )
    p.add_argument("--table", default="FAD", choices=list(config.TABLES))
    p.add_argument("--block", default="family", choices=["family", "employment"])
    p.add_argument("--out", default=None, help="ruta del CSV de salida (por defecto reports/eval/model_comparison.csv)")
    p.add_argument(
        "--models",
        nargs="+",
        default=list(config.MODEL_NAMES),
        choices=list(config.MODEL_NAMES),
        help="subconjunto de modelos a comparar",
    )
    p.add_argument("--mlflow", action="store_true", help="loguear cada backtest a tracking (JSONL -> MLflow)")
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help="permite correr con el árbol git sucio (corrida exploratoria, NO canónica)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    config.seed_everything()
    countries = config.PILOT_COUNTRIES if args.country == "all" else (args.country,)
    cat = dataset.list_series(table=args.table, block=args.block, countries=countries)
    series = [(r.country, r.category, r.table) for r in cat.itertuples()]
    meta = config.run_metadata()
    if meta["git_dirty"] and not args.allow_dirty:
        # AO3: a canonical campaign on a dirty tree is irreproducible — the git sha
        # in the ledger would not correspond to the code that actually ran.
        raise SystemExit(
            "ABORT: dirty git tree — a campaign run would be irreproducible. "
            "Commit your changes or pass --allow-dirty for an exploratory run."
        )
    log.info(
        "run_id=%s · %d modelos × %d series (%s/%s/%s) · git=%s%s",
        meta["run_id"],
        len(args.models),
        len(series),
        args.country,
        args.table,
        args.block,
        (meta["git_sha"] or "?")[:7],
        " (dirty)" if meta["git_dirty"] else "",
    )

    df = run(series, tuple(args.models), meta["run_id"], track=args.mlflow)
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT
    # Contrato: OVERWRITE (una sola corrida por CSV). Los consumidores igual filtran
    # run_id == max() como defensa por si algún flujo futuro acumula corridas.
    df.to_csv(out, index=False)

    # Ledger append-only: procedencia + resumen de esta corrida.
    meta.update(
        {
            "args": vars(args),
            "n_results": len(df),
            "summary": summary(df).reset_index().to_dict(orient="records") if len(df) else [],
        }
    )
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(meta) + "\n")

    log.info(
        "Resultados -> %s (%d corridas) · ledger -> %s",
        out.relative_to(REPORTS.parent) if out.is_relative_to(REPORTS.parent) else out,
        len(df),
        LEDGER.relative_to(REPORTS.parent),
    )
    if len(df):
        log.info("Ranking por MASE de selección:\n%s", summary(df).to_string())


if __name__ == "__main__":
    main()
