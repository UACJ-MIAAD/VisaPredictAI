"""Confirma en HOLD-OUT las ganancias del tuning (US-O3, regla anti-overtuning).

El tuner optimiza un objetivo de validación interna; eso NO basta para aceptar (Schneider
2025: el riesgo es sobre-ajustar al criterio de selección). Aquí se corre el walk-forward
COMPLETO con los hiperparámetros tuneados sobre cada serie del grupo y se compara el MASE
de HOLD-OUT contra el de los defaults (tomado del CSV de 21 modelos). Solo se ACEPTA el
tuning si mejora en hold-out; si no, se conserva el default. Escribe
``reports/tuning_confirmation.csv``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from vp_model import dataset, significance, tune, walkforward
from vp_model.config import get_logger

log = get_logger("confirm_tuning")
REPORTS = Path(__file__).resolve().parent.parent / "reports"


def _default_holdout(model: str, table: str, block: str) -> dict[tuple[str, str], float]:
    """hold_mase por (país, categoría) de los defaults, del CSV de 21 modelos."""
    df = pd.read_csv(REPORTS / f"model_comparison_{table}21.csv")
    d = df[df.model == model]
    return {(r.country, r.category): r.hold_mase for r in d.itertuples()}


def confirm() -> pd.DataFrame:
    tuned = json.loads((REPORTS / "tuned_params.json").read_text())
    rows = []
    for model, groups in tuned.items():
        for key, info in groups.items():
            table, block = key.split("_")
            params = info["best_params"]
            defaults = _default_holdout(model, table, block)
            cat = dataset.list_series(table=table, block=block)
            for r in cat.itertuples():
                try:
                    m = tune._build_tuned(model, dict(params))
                    res = walkforward.backtest(model, r.country, r.category, table, model=m)
                    rows.append(
                        {
                            "model": model,
                            "table": table,
                            "country": r.country,
                            "category": r.category,
                            "tuned_hold_mase": res.holdout["mase"],
                            "default_hold_mase": defaults.get((r.country, r.category), np.nan),
                        }
                    )
                except Exception as e:  # noqa: BLE001 — serie que falle no aborta el resto
                    log.warning("skip %s %s/%s: %s", model, r.country, r.category, e)
            log.info("confirmado %s · %s", model, key)
    df = pd.DataFrame(rows)
    df.to_csv(REPORTS / "tuning_confirmation.csv", index=False)
    return df


def summary(df: pd.DataFrame) -> pd.DataFrame:
    """Media de hold-MASE tuned vs default por modelo×tabla + veredicto de aceptación.

    B2: pseudo-réplicas del corte mundial colapsadas antes de promediar.
    """
    if {"country", "category", "model"} <= set(df.columns):
        df, n_raw, n_eff = significance.dedup_series(df, value="default_hold_mase")
        if n_eff < n_raw:
            log.info("dedup pseudo-réplicas: %d series -> %d efectivas", n_raw, n_eff)
    g = (
        df.groupby(["model", "table"])
        .agg(tuned=("tuned_hold_mase", "mean"), default=("default_hold_mase", "mean"), n=("country", "count"))
        .reset_index()
    )
    g["delta_pct"] = (100 * (g.default - g.tuned) / g.default).round(1)
    g["acepta"] = g.tuned < g.default
    return g.round(4)


if __name__ == "__main__":
    s = summary(confirm())
    print(s.to_string(index=False))
    print("\nACEPTAR tuning donde acepta=True; conservar default en el resto (regla US-O3).")
