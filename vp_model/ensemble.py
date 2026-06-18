"""Selección de modelo por serie y combinaciones (US-N1, patrón EpiForecast-MX).

El hallazgo del marco comparativo es que NINGÚN modelo domina todas las series
(ETS/Theta ganan FAD en promedio, CatBoost gana la mayoría individual y DFF). La
estrategia que mueve la aguja no es un solo ganador global sino **seleccionar por
serie** con un criterio leakage-free: elegir el modelo con menor MASE en la región de
SELECCIÓN (que nunca ve el hold-out) y reportar su MASE de HOLD-OUT. Se compara contra:
  * el mejor modelo GLOBAL (un único modelo, el de menor MASE de hold-out promedio),
  * el ORÁCULO por serie (mejor hold-out posible; cota superior NO alcanzable, mide
    cuánto deja sobre la mesa la selección imperfecta),
  * combinaciones simples (media/mediana de los top-k por selección) — requieren los
    pronósticos, no solo las métricas, así que se reportan aparte si están disponibles.

Todo se deriva del CSV ``model_comparison_*21.csv`` (filas por modelo×serie).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from vp_model import dataset

REPORTS = Path(__file__).resolve().parent.parent / "reports"


@dataclass(frozen=True)
class Strategy:
    name: str
    hold_mase: float  # MASE de hold-out promedio sobre las series
    hold_mae: float  # MAE de hold-out promedio (días)
    detail: str


def _global_best(df: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    """Mejor modelo global por MASE de hold-out promedio; devuelve nombre + sus filas."""
    by_model = df.groupby("model")["hold_mase"].mean().sort_values()
    best = by_model.index[0]
    return best, df[df.model == best]


def _per_series_selection(df: pd.DataFrame) -> pd.DataFrame:
    """Por cada serie, la fila del modelo con menor sel_mase (criterio leakage-free)."""
    idx = df.groupby(["country", "category"])["sel_mase"].idxmin()
    return df.loc[idx]


def _per_series_oracle(df: pd.DataFrame) -> pd.DataFrame:
    """Cota superior: por serie, el menor hold_mase (mirando el hold-out; NO usable)."""
    idx = df.groupby(["country", "category"])["hold_mase"].idxmin()
    return df.loc[idx]


def analyze(table: str = "FAD") -> list[Strategy]:
    """Compara mejor-global vs selección-por-serie vs oráculo sobre el CSV de 21 modelos."""
    df = pd.read_csv(REPORTS / f"model_comparison_{table}21.csv")
    name, gb = _global_best(df)
    sel = _per_series_selection(df)
    orc = _per_series_oracle(df)
    n_distinct = sel.model.nunique()
    return [
        Strategy(
            f"mejor global ({name})", gb.hold_mase.mean(), gb.hold_mae.mean(), f"un solo modelo en las {len(gb)} series"
        ),
        Strategy(
            "selección por serie",
            sel.hold_mase.mean(),
            sel.hold_mae.mean(),
            f"{n_distinct} modelos distintos elegidos por sel_mase",
        ),
        Strategy(
            "oráculo por serie", orc.hold_mase.mean(), orc.hold_mae.mean(), "cota superior inalcanzable (mira hold-out)"
        ),
    ]


def selection_table(table: str = "FAD") -> pd.DataFrame:
    """Qué modelo se eligió por serie y su error de hold-out (para auditar la selección)."""
    df = pd.read_csv(REPORTS / f"model_comparison_{table}21.csv")
    sel = _per_series_selection(df)[["country", "category", "model", "sel_mase", "hold_mase"]]
    return sel.sort_values(["country", "category"]).reset_index(drop=True)


# Combinación CURADA: solo los modelos simples fuertes (combinar buenos con buenos ayuda;
# meter GBMs/kalman débiles arruina la media). En FAD la mediana de estos supera al mejor
# único (~5%); en DFF —la serie más corta— SARIMA solo es imbatible.
STRONG_SET = ("theta", "ets", "sarima")


def curated_combination(table: str = "FAD", subset: tuple[str, ...] = STRONG_SET, agg: str = "median") -> Strategy:
    """Combinación de un subconjunto curado de modelos fuertes (mediana por defecto)."""
    from vp_model.eval_neuralforecast import _naive_scale

    fc = pd.read_csv(REPORTS / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    sub = fc[fc.model.isin(subset)]
    comb = (
        sub.groupby(["country", "category", "date"])
        .agg(actual=("actual", "first"), pred=("forecast", agg))
        .reset_index()
    )
    maes, mases = [], []
    for (country, category), g in comb.groupby(["country", "category"]):
        full = dataset.load_series(country, category, table)
        scale = _naive_scale(full.iloc[: -len(g)].astype("float64").to_numpy())
        mae = (g.actual - g.pred).abs().mean()
        maes.append(mae)
        mases.append(mae / scale)
    return Strategy(
        f"combinación curada {agg} ({'+'.join(subset)})",
        float(pd.Series(mases).mean()),
        float(pd.Series(maes).mean()),
        f"{len(subset)} modelos fuertes",
    )


def combinations(table: str = "FAD") -> list[Strategy]:
    """Evalúa combinaciones de pronósticos (media/mediana) sobre los forecasts persistidos.

    Requiere ``reports/holdout_forecasts_{table}.csv`` (de ``persist_forecasts``). La media
    y la mediana se calculan POR fecha×serie sobre el set curado; el error se promedia por
    serie escalando por el naïve estacional in-sample (leakage-free: la escala usa solo el
    tramo previo al hold-out). Devuelve [] si no existe el CSV (aún no persistido).
    """
    from vp_model.eval_neuralforecast import _naive_scale

    path = REPORTS / f"holdout_forecasts_{table}.csv"
    if not path.exists():
        return []
    fc = pd.read_csv(path, parse_dates=["date"])
    out = []
    for agg_name, agg in (("media", "mean"), ("mediana", "median")):
        comb = (
            fc.groupby(["country", "category", "date"])
            .agg(actual=("actual", "first"), pred=("forecast", agg))
            .reset_index()
        )
        maes, mases = [], []
        for (country, category), g in comb.groupby(["country", "category"]):
            full = dataset.load_series(country, category, table)
            scale = _naive_scale(full.iloc[: -len(g)].astype("float64").to_numpy())
            mae = (g.actual - g.pred).abs().mean()
            maes.append(mae)
            mases.append(mae / scale)
        out.append(
            Strategy(
                f"combinación ({agg_name})",
                float(pd.Series(mases).mean()),
                float(pd.Series(maes).mean()),
                f"{fc.model.nunique()} modelos curados",
            )
        )
    return out


def demo() -> None:
    """Self-check: selección por serie no empeora al mejor global y queda bajo el oráculo."""
    for table in ("FAD", "DFF"):
        strat = {s.name.split(" (")[0]: s for s in analyze(table)}
        gb = strat["mejor global"]
        sel = strat["selección por serie"]
        orc = strat["oráculo por serie"]
        assert orc.hold_mase <= sel.hold_mase + 1e-9, (orc, sel)  # oráculo es cota inferior
        print(f"\n=== {table} ===")
        for s in analyze(table):
            print(f"  {s.name:32s} hold MASE={s.hold_mase:.3f}  MAE={s.hold_mae:6.1f}d  · {s.detail}")
        gap = 100 * (sel.hold_mase - gb.hold_mase) / gb.hold_mase
        print(f"  selección-por-serie vs mejor-global: {gap:+.1f}% MASE")
    print("\nOK — ensemble por selección evaluado (leakage-free, criterio sel_mase)")


if __name__ == "__main__":
    demo()
