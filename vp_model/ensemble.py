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

AM1: la mediana-de-los-mejores-K por serie (K elegido por ``sel_mase``, leakage-free)
está implementada en ``best_k_combination``/``best_k_report`` — antes este docstring la
prometía y nunca existió. AM4b/AM4d: todos los reportes de combinaciones puntúan con
``metrics.mase_by_series`` (máscara F-only canónica) sobre el denominador DEDUPLICADO
de representantes de pseudo-réplica (``champion.replica_representatives``), el mismo
que usa el gate campeón-retador.

Todo se deriva de ``model_comparison_*21.csv`` (métricas por modelo×serie) y de
``holdout_forecasts_*.csv`` (pronósticos persistidos del hold-out).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from vp_model import significance

REPORTS = Path(__file__).resolve().parent.parent / "reports"


@dataclass(frozen=True)
class Strategy:
    name: str
    hold_mase: float  # MASE de hold-out promedio sobre las series
    hold_mae: float  # MAE de hold-out promedio (días)
    detail: str


def representative_filter(comb: pd.DataFrame, table: str, fc: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Restrict a combined-forecast frame to one series per pseudo-replica class (AM4b).

    Uniform denominator across ALL ensemble reports: the champion gate scores 15 (FAD) /
    10 (DFF) effective series while the old combination loops averaged over the raw 25 —
    an internal rule-#0 violation (25-vs-15 in the same document). ``fc`` is the persisted
    hold-out frame whose ``actual`` signature defines the replica classes.
    Returns (filtered frame, n_raw, n_effective).
    """
    from vp_model.champion import replica_representatives

    reps = set(replica_representatives(table, fc))
    n_raw = comb.groupby(["country", "category"]).ngroups
    out = comb[[(c, k) in reps for c, k in zip(comb.country, comb.category, strict=True)]]
    return out, n_raw, out.groupby(["country", "category"]).ngroups


def _score_combined(comb: pd.DataFrame, table: str) -> tuple[float, float, int]:
    """Mean hold-out (MASE, MAE, n_series) of a combined frame via the canonical scorer.

    MASE comes from ``metrics.mase_by_series`` (F-only mask + shared naive scale, AM4d)
    using the persisted ``actual`` column as ground truth; MAE is the per-series mean of
    the same masked residuals' absolute values, averaged across series.
    """
    from vp_model.metrics import mase_by_series

    mases = mase_by_series(comb, table, pred_col="pred", actual_col="actual")
    maes = (comb.actual - comb.pred).abs().groupby([comb.country, comb.category]).mean()
    return float(mases.mean()), float(maes.mean()), int(mases.count())


def _global_best(df: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    """Mejor modelo global elegido por MASE de SELECCIÓN; se reporta su hold-out.

    B5: elegir por ``hold_mase`` y reportar ese mismo ``hold_mase`` era selección
    sobre el test set (cota optimista, incomparable con "selección por serie", que
    sí elige leakage-free por ``sel_mase``)."""
    by_model = df.groupby("model")["sel_mase"].mean().sort_values()
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
    df = pd.read_csv(REPORTS / "eval" / f"model_comparison_{table}21.csv").pipe(lambda d: d[d.run_id == d.run_id.max()])
    # B2: las medias de las estrategias no deben sobreponderar el corte mundial. La firma
    # métrica (dedup_series) NO detecta estas réplicas (comparten el corte reciente pero
    # no la historia → la escala del MASE difiere); la firma por `actual` del hold-out sí.
    try:
        from vp_model.champion import replica_representatives

        reps = set(replica_representatives(table))
        n_raw = df.groupby(["country", "category"]).ngroups
        df = df[[(c, cat) in reps for c, cat in zip(df.country, df.category, strict=True)]]
        n_eff = df.groupby(["country", "category"]).ngroups
    except FileNotFoundError:  # sin holdout_forecasts_*: fallback a la firma métrica
        df, n_raw, n_eff = significance.dedup_series(df, value="hold_mase")
    if n_eff < n_raw:
        print(f"[ensemble] dedup pseudo-réplicas: {n_raw} series -> {n_eff} efectivas")
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
    df = pd.read_csv(REPORTS / "eval" / f"model_comparison_{table}21.csv").pipe(lambda d: d[d.run_id == d.run_id.max()])
    sel = _per_series_selection(df)[["country", "category", "model", "sel_mase", "hold_mase"]]
    return sel.sort_values(["country", "category"]).reset_index(drop=True)


# Curated combination: only the strong simple models (combining good with good helps;
# weak GBMs/kalman drag the mean down). AM4c: the old claim here ("in FAD the median of
# these beats the best single model by ~5%") is FALSE post-resurrection (3-jul-2026):
# median{theta,ets,sarima} 0.1136 vs Theta 0.113 hold MASE = a TIE. In DFF (the short
# table) SARIMA alone remains unbeaten. The serious candidate is best-K per series (AM1).
STRONG_SET = ("theta", "ets", "sarima")


def curated_combination(table: str = "FAD", subset: tuple[str, ...] = STRONG_SET, agg: str = "median") -> Strategy:
    """Combinación de un subconjunto curado de modelos fuertes (mediana por defecto).

    AM4b/AM4d: scored with ``metrics.mase_by_series`` over deduplicated replica
    representatives (same denominator as the champion gate)."""
    fc = pd.read_csv(REPORTS / "eval" / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    sub = fc[fc.model.isin(subset)]
    comb = (
        sub.groupby(["country", "category", "date"])
        .agg(actual=("actual", "first"), pred=("forecast", agg))
        .reset_index()
    )
    comb, _n_raw, n_eff = representative_filter(comb, table, fc)
    mase_mean, mae_mean, _n = _score_combined(comb, table)
    return Strategy(
        f"combinación curada {agg} ({'+'.join(subset)})",
        mase_mean,
        mae_mean,
        f"{len(subset)} modelos fuertes · {n_eff} series efectivas",
    )


def combinations(table: str = "FAD") -> list[Strategy]:
    """Evalúa combinaciones de pronósticos (media/mediana) sobre los forecasts persistidos.

    Requiere ``reports/eval/holdout_forecasts_{table}.csv`` (de ``persist_forecasts``). La media
    y la mediana se calculan POR fecha×serie sobre el set curado; el error se puntúa con
    ``metrics.mase_by_series`` (F-only, escala naïve leakage-free) sobre el denominador
    deduplicado (AM4b/AM4d). Devuelve [] si no existe el CSV (aún no persistido).
    """
    path = REPORTS / "eval" / f"holdout_forecasts_{table}.csv"
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
        comb, _n_raw, n_eff = representative_filter(comb, table, fc)
        mase_mean, mae_mean, _n = _score_combined(comb, table)
        out.append(
            Strategy(
                f"combinación ({agg_name})",
                mase_mean,
                mae_mean,
                f"{fc.model.nunique()} modelos curados · {n_eff} series efectivas",
            )
        )
    return out


def best_k_combination(
    table: str, k: int, fc: pd.DataFrame | None = None, mc: pd.DataFrame | None = None
) -> tuple[Strategy, pd.DataFrame]:
    """AM1 — median-of-best-K per series, chosen leakage-free on the SELECTION region.

    For each series, take the ``k`` models with the lowest ``sel_mase`` (selection-region
    MASE from ``model_comparison_{table}21.csv`` — never the hold-out, never a
    retrospective STRONG_SET) among the models whose hold-out forecasts are persisted,
    and median-combine their hold-out forecasts. Scored with the canonical F-only scorer
    over deduplicated replica representatives (AM4b/AM4d).

    Returns (aggregate Strategy, per-series frame with the chosen models and MASE).
    """
    if fc is None:
        fc = pd.read_csv(REPORTS / "eval" / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    if mc is None:
        mc = pd.read_csv(REPORTS / "eval" / f"model_comparison_{table}21.csv").pipe(
            lambda d: d[d.run_id == d.run_id.max()]
        )
    avail = set(fc.model.unique())
    sel = mc[mc.model.isin(avail)].dropna(subset=["sel_mase"])
    picks: dict[tuple[str, str], list[str]] = {
        key: g.nsmallest(k, "sel_mase").model.tolist() for key, g in sel.groupby(["country", "category"])
    }
    parts = []
    for (country, category), chosen in sorted(picks.items()):
        sub = fc[(fc.country == country) & (fc.category == category) & fc.model.isin(chosen)]
        if sub.empty:
            continue
        comb = (
            sub.groupby(["country", "category", "date"])
            .agg(actual=("actual", "first"), pred=("forecast", "median"))
            .reset_index()
        )
        comb["models"] = "+".join(sorted(chosen))
        parts.append(comb)
    allc = pd.concat(parts, ignore_index=True)
    allc, _n_raw, n_eff = representative_filter(allc, table, fc)
    from vp_model.metrics import mase_by_series

    per_series = mase_by_series(allc, table, pred_col="pred", actual_col="actual").rename("hold_mase").reset_index()
    per_series["models"] = [
        allc.loc[(allc.country == r.country) & (allc.category == r.category), "models"].iloc[0]
        for r in per_series.itertuples()
    ]
    mase_mean, mae_mean, _n = _score_combined(allc, table)
    strat = Strategy(
        f"best-{k} por serie (mediana, sel_mase)",
        mase_mean,
        mae_mean,
        f"K={k} elegidos por selección · {n_eff} series efectivas",
    )
    return strat, per_series


def best_k_report(table: str, ks: tuple[int, ...] = (2, 3, 5)) -> pd.DataFrame:
    """Per-series + aggregate best-K results for ``ks`` (long frame, ready for CSV).

    Aggregate rows carry ``country == "ALL"`` (same convention as the CRPS report) with
    the mean hold-out MASE/MAE over the deduplicated effective series.
    """
    fc = pd.read_csv(REPORTS / "eval" / f"holdout_forecasts_{table}.csv", parse_dates=["date"])
    mc = pd.read_csv(REPORTS / "eval" / f"model_comparison_{table}21.csv").pipe(lambda d: d[d.run_id == d.run_id.max()])
    rows = []
    for k in ks:
        strat, per_series = best_k_combination(table, k, fc=fc, mc=mc)
        for r in per_series.itertuples():
            rows.append(
                {
                    "table": table,
                    "k": k,
                    "country": r.country,
                    "category": r.category,
                    "models": r.models,
                    "hold_mase": round(float(r.hold_mase), 4),
                    "hold_mae": float("nan"),
                }
            )
        rows.append(
            {
                "table": table,
                "k": k,
                "country": "ALL",
                "category": "",
                "models": "",
                "hold_mase": round(strat.hold_mase, 4),
                "hold_mae": round(strat.hold_mae, 2),
            }
        )
    return pd.DataFrame(rows)


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
