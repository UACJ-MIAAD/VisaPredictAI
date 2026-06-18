"""Evalúa los pronósticos globales de neuralforecast con NUESTRAS métricas.

Lee ``reports/neuralforecast_forecasts.csv`` (producido en el venv aislado ``ante_nf``
por ``run_neuralforecast.py``) y calcula MASE/sMAPE/MAE/RMSE sobre el hold-out para
cada modelo global (PatchTST, iTransformer, NHITS...), de forma comparable al pool
local. Cierra el puente entre los dos entornos: entrenamiento global en pandas<3,
evaluación unificada en pandas 3.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vp_model.metrics import naive_scale_before

REPORTS = Path(__file__).resolve().parent.parent / "reports"
CSV = REPORTS / "neuralforecast_forecasts.csv"
NON_MODEL = {"index", "unique_id", "ds", "cutoff", "y"}


def evaluate(csv: Path = CSV) -> pd.DataFrame:
    """Métricas de hold-out por modelo global × serie a partir del CSV de forecasts."""
    from vp_model import dataset

    fc = pd.read_csv(csv, parse_dates=["ds"])
    models = [c for c in fc.columns if c not in NON_MODEL]
    rows = []
    for uid, g in fc.groupby("unique_id"):
        country, category = uid.split("/")
        full = dataset.load_series(country, category, "FAD").astype("float64")  # escala del train
        g = g.sort_values("ds")
        scale = naive_scale_before(full, g["ds"].min())  # por fecha, fuente única
        y = g["y"].to_numpy()
        for m in models:
            f = g[m].to_numpy()
            mae = float(np.mean(np.abs(y - f)))
            rows.append(
                {
                    "model": m,
                    "country": country,
                    "category": category,
                    "table": "FAD",
                    "hold_mae": mae,
                    "hold_rmse": float(np.sqrt(np.mean((y - f) ** 2))),
                    "hold_smape": float(np.mean(2 * np.abs(y - f) / (np.abs(y) + np.abs(f) + 1e-9))),
                    "hold_mase": mae / scale,
                    "n": len(y),
                }
            )
    return pd.DataFrame(rows)


def summary(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df.groupby("model")[["hold_mase", "hold_smape", "hold_mae", "hold_rmse"]]
        .mean()
        .sort_values("hold_mase")
        .round(3)
    )


def eval_global_deep(table: str = "FAD") -> pd.DataFrame:
    """Evalúa los CSV de ``run_global_deep`` (niveles y/o diff) con MASE por serie.

    Lee ``reports/global_{table}_{levels,diff}.csv`` (unique_id=país/bloque/categoría,
    ds, y real, columnas de modelo en NIVEL) y calcula MASE de hold-out por serie con la
    MISMA escala naïve estacional que el pool local, para comparar de forma justa contra
    ETS/Theta. Devuelve un DataFrame largo (variante, modelo, bloque, serie, MASE, sMAPE).
    """
    from vp_model import dataset

    rows = []
    for path in sorted(REPORTS.glob(f"global_{table}_*.csv")):
        variant = path.stem.replace(f"global_{table}_", "")
        df = pd.read_csv(path, parse_dates=["ds"])
        models = [c for c in df.columns if c not in ("unique_id", "ds", "y")]
        for uid, g in df.groupby("unique_id"):
            country, block, category = uid.split("/")
            try:
                full = dataset.load_series(country, category, table).astype("float64")
            except KeyError:
                continue
            g = g.sort_values("ds")
            if (full.index < g["ds"].min()).sum() == 0:
                continue  # sin historia previa al hold-out: serie no evaluable
            scale = naive_scale_before(full, g["ds"].min())  # escala única, por fecha
            # M1: evaluar SOLO meses F reales. El CSV global trae el nivel C-encoded para dar
            # continuidad al entrenamiento; en empleo ~23% del hold-out son C interpolado, que
            # es función determinista de t y deprimiría el MASE. ``full`` (F-only) marca los F.
            fmask = g["ds"].isin(full.index).to_numpy()
            if not fmask.any():
                continue
            y = g["y"].to_numpy()[fmask]
            for m in models:
                f = g[m].to_numpy()[fmask]
                if np.isnan(f).all():
                    continue
                mae = float(np.nanmean(np.abs(y - f)))
                rows.append(
                    {
                        "variant": variant,
                        "model": m,
                        "block": block,
                        "country": country,
                        "category": category,
                        "hold_mase": mae / scale,
                        "hold_smape": float(np.nanmean(2 * np.abs(y - f) / (np.abs(y) + np.abs(f) + 1e-9))),
                        "hold_mae": mae,
                    }
                )
    return pd.DataFrame(rows)


def global_summary(df: pd.DataFrame, block: str = "family") -> pd.DataFrame:
    """Ranking de los modelos globales sobre un bloque (familiar por defecto) vs el listón."""
    sub = df[df.block == block]
    return (
        sub.groupby(["variant", "model"])[["hold_mase", "hold_smape", "hold_mae"]]
        .mean()
        .sort_values("hold_mase")
        .round(4)
    )


def demo() -> None:
    """Self-check: el CSV global se evalúa y produce MASE por modelo."""
    df = evaluate()
    assert not df.empty and {"PatchTST", "NHITS"} <= set(df["model"])
    s = summary(df)
    assert (s["hold_mase"] > 0).all()
    print("OK — neuralforecast global, escala naïve estacional; ranking hold-out:")
    print(s.to_string())


if __name__ == "__main__":
    demo()
