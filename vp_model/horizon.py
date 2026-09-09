"""Campeón POR HORIZONTE — walk-forward multi-horizonte con orígenes RODANTES.

Corrige el sesgo época×horizonte del backtest de ventana fija que la auditoría del
showdown deep GPU (jul-2026, memoria ``project_gpu_multihorizon_showdown``) destapó:
``darts.historical_forecasts`` con ``forecast_horizon>1`` y ``last_points_only=False``
produce, en CADA origen (ventana expansible desde ``MIN_TRAIN``), un pronóstico de
h=1..H pasos; el paso k alimenta el horizonte h=k. Al rodar los orígenes por TODO el
span (no solo el hold-out de 24 meses) el horizonte queda desacoplado de la época.

Se puntúa SOLO sobre fechas F reales (mismo contrato que ``walkforward``) con la MISMA
escala MASE canónica (:func:`metrics.naive_scale_before`, naïve estacional train-before).
El campeón se elige por horizonte. A h=1 el random walk (``naive1``) es el piso; **cuál gana a
horizontes largos se lee de ``reports/eval/horizon_facts.json``** (`champion_by_h`), no de este
docstring: escribir aquí un nombre lo vuelve fósil en cuanto el corte cambia.

Alcance: modelos CLÁSICOS (:data:`config.HORIZON_CANDIDATES`). El frontier deep no
aportó skill honesto (misma memoria); además los clásicos reentrenan en cada origen
(rolling verdadero) a bajo costo, mientras las redes están fijadas a ``forecast_horizon=1``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import wilcoxon

from vp_model import dataset, metrics, models, significance
from vp_model.config import (
    HOLDOUT,
    HORIZON_CANDIDATES,
    HORIZONS,
    MIN_TRAIN,
    RETRAIN_EACH_STEP,
    get_logger,
)
from vp_model.feature_builder import FeatureBuilder

log = get_logger(__name__)


def _block(category: str) -> str:
    """Bloque de grupo de tuning desde el código de categoría (EB* -> employment)."""
    return "employment" if category.upper().startswith("EB") else "family"


def forecasts_by_horizon(model_name: str, country: str, category: str, table: str, hmax: int) -> dict[int, pd.Series]:
    """``{h: Series(fecha_objetivo -> pronóstico)}`` — orígenes rodantes, leakage-free.

    Cada origen de la ventana expansible (desde ``MIN_TRAIN[table]``) emite un
    pronóstico de ``hmax`` pasos; solo ve el pasado. El paso k del pronóstico se
    asigna al horizonte h=k con su fecha objetivo (origen + k meses).
    """
    fe = FeatureBuilder(model_name)
    raw = dataset.load_series(country, category, table)
    ts = fe.to_timeseries(raw)
    min_train = MIN_TRAIN[table]
    # Cap por-serie: una serie que no alcanza hmax NO se descarta — contribuye hasta su
    # horizonte factible (así extender el grid a h=36 no sesga los horizontes cortos al
    # dropar las series más cortas). Los horizontes largos los sostienen las series largas.
    eff_hmax = min(hmax, len(ts) - min_train)
    if eff_hmax < 1:
        raise ValueError(f"serie corta ({len(ts)}) para min_train={min_train} (ni h=1)")
    model = models.build_model(model_name, table=table, block=_block(category))
    retrain: bool | int = model_name in RETRAIN_EACH_STEP
    cov = fe.covariates(ts, raw)  # F1: las máscaras MNAR requieren la serie F cruda
    extra: dict[str, object] = {"future_covariates": cov} if cov is not None else {}
    per_origin = model.historical_forecasts(
        ts,
        start=min_train,
        forecast_horizon=eff_hmax,
        stride=1,
        retrain=retrain,
        last_points_only=False,  # devuelve un TimeSeries por origen (longitud hmax)
        verbose=False,
        **extra,
    )
    out: dict[int, dict] = {h: {} for h in range(1, hmax + 1)}
    for fc in per_origin:
        vals = np.asarray(fc.values()).ravel()
        idx = fc.time_index
        for k in range(min(hmax, len(vals))):
            out[k + 1][idx[k]] = float(vals[k])
    return {h: pd.Series(d).sort_index() for h, d in out.items() if d}


def mase_by_horizon(
    model_name: str,
    country: str,
    category: str,
    table: str,
    hmax: int,
    window: str = "all",
) -> dict[int, float]:
    """``{h: MASE}`` F-only de un modelo sobre una serie, escala canónica train-before.

    La escala del MASE es la ÚNICA fuente del proyecto: naïve estacional in-sample
    sobre la serie F cruda anterior al hold-out (idéntica a ``walkforward.backtest``),
    de modo que los MASE por horizonte son comparables con las cifras canónicas.

    ``window`` parte los OBJETIVOS en el tiempo, que es lo que hace posible un router
    leakage-free (E4): ``"selection"`` puntúa solo objetivos anteriores al hold-out y
    ``"holdout"`` solo los del hold-out. ``"all"`` conserva el comportamiento histórico.
    """
    if window not in {"all", "selection", "holdout"}:
        raise ValueError(f"window debe ser all, selection u holdout, no {window!r}")
    raw = dataset.load_series(country, category, table).astype("float64")
    ts = FeatureBuilder(model_name).to_timeseries(dataset.load_series(country, category, table))
    split = ts.time_index[-HOLDOUT]
    scale = metrics.naive_scale_before(raw, split)
    if not np.isfinite(scale) or scale == 0:
        return {}
    fmask = set(raw.index)  # fechas F reales (único objetivo puntuable, B1)
    res: dict[int, float] = {}
    for h, s in forecasts_by_horizon(model_name, country, category, table, hmax).items():
        common = [d for d in s.index if d in fmask]
        if window == "selection":
            common = [d for d in common if d < split]
        elif window == "holdout":
            common = [d for d in common if d >= split]
        if not common:
            continue
        pred = s.loc[common].to_numpy()
        actual = raw.loc[common].to_numpy()
        res[h] = float(np.mean(np.abs(actual - pred)) / scale)
    return res


def evaluable(table: str) -> list[tuple[str, str]]:
    """``(country, category)`` evaluables de una tabla, del catálogo canónico (mart)."""
    cat = dataset.evaluable_series()
    cat = cat[cat["table"] == table]
    return list(zip(cat.country, cat.category, strict=True))


def champion_by_horizon(
    table: str, candidates: tuple[str, ...] = HORIZON_CANDIDATES, hmax: int | None = None
) -> pd.DataFrame:
    """Tabla MASE-por-horizonte (media por serie) + columna ``champion`` por horizonte.

    Índice = horizontes de :data:`config.HORIZONS`; columnas = ``candidates`` (clásicos);
    valores = MASE medio sobre las series evaluables. La columna ``champion`` es el
    modelo de menor MASE a ese horizonte (el random walk a h=1; la parsimonia a h largos).
    """
    hmax = hmax or max(HORIZONS)
    series = evaluable(table)
    acc: dict[str, dict[int, list[float]]] = {m: {h: [] for h in range(1, hmax + 1)} for m in candidates}
    for m in candidates:
        for country, category in series:
            try:
                mh = mase_by_horizon(m, country, category, table, hmax)
            except (ValueError, IndexError, KeyError) as exc:
                log.warning("horizonte %s/%s/%s/%s omitido (%s)", m, country, category, table, exc)
                continue
            for h, v in mh.items():
                acc[m][h].append(v)
    rows = []
    for h in HORIZONS:
        row: dict[str, object] = {"h": h}
        for m in candidates:
            vals = acc[m].get(h, [])
            row[m] = float(np.mean(vals)) if vals else np.nan
        # nº de series que sostienen ESTE horizonte (cae en h largos: sirve para saber
        # hasta dónde el veredicto es confiable vs solo unas pocas series largas).
        row["n"] = max((len(acc[m].get(h, [])) for m in candidates), default=0)
        rows.append(row)
    df = pd.DataFrame(rows).set_index("h")
    df["champion"] = df[list(candidates)].idxmin(axis=1)
    return df


@dataclass(frozen=True)
class RouterRecipe:
    """Router por cohorte: qué candidato usa cada cohorte a un horizonte dado.

    La elección sale de la ventana de SELECCIÓN (objetivos anteriores al hold-out) y se evalúa
    solo sobre el hold-out, así que el router nunca ve el dato con el que se le juzga. La receta
    es un dato inmutable: quien la construye no puede reabrirla después de ver el resultado.
    """

    table: str
    horizon: int
    by_cohort: Mapping[str, str]
    selection_window: str = "selection"

    @property
    def name(self) -> str:
        elegidos = ", ".join(f"{c}→{m}" for c, m in sorted(self.by_cohort.items()))
        return f"router[{self.table}|h={self.horizon}|{elegidos}]"


def mase_grid(
    table: str,
    series: list[tuple[str, str]],
    candidates: tuple[str, ...],
    hmax: int,
) -> dict[tuple[str, str, str], dict[str, dict[int, float]]]:
    """``{(modelo, país, categoría): {ventana: {h: MASE}}}`` con UNA sola pasada de pronóstico.

    Calcular selección y hold-out por separado duplicaría el ajuste de cada modelo en cada
    origen; aquí los pronósticos se producen una vez y las dos ventanas se derivan de ellos.
    """
    fuera: dict[tuple[str, str, str], dict[str, dict[int, float]]] = {}
    for modelo in candidates:
        for país, categoría in series:
            try:
                raw = dataset.load_series(país, categoría, table).astype("float64")
                ts = FeatureBuilder(modelo).to_timeseries(dataset.load_series(país, categoría, table))
                split = ts.time_index[-HOLDOUT]
                escala = metrics.naive_scale_before(raw, split)
                if not np.isfinite(escala) or escala == 0:
                    continue
                fmask = set(raw.index)
                por_ventana: dict[str, dict[int, float]] = {"selection": {}, "holdout": {}}
                for h, serie in forecasts_by_horizon(modelo, país, categoría, table, hmax).items():
                    objetivos = [d for d in serie.index if d in fmask]
                    for ventana, sel in (
                        ("selection", [d for d in objetivos if d < split]),
                        ("holdout", [d for d in objetivos if d >= split]),
                    ):
                        if not sel:
                            continue
                        pred = serie.loc[sel].to_numpy()
                        real = raw.loc[sel].to_numpy()
                        por_ventana[ventana][h] = float(np.mean(np.abs(real - pred)) / escala)
                fuera[(modelo, país, categoría)] = por_ventana
            except (ValueError, IndexError, KeyError) as exc:
                log.warning("rejilla %s/%s/%s/%s omitida (%s)", modelo, país, categoría, table, exc)
    return fuera


def fit_router(
    table: str,
    h: int,
    cohorts: Mapping[tuple[str, str], str],
    grid: dict[tuple[str, str, str], dict[str, dict[int, float]]],
    candidates: tuple[str, ...] = HORIZON_CANDIDATES,
) -> RouterRecipe:
    """Elige, por cohorte, el candidato de MENOR MASE medio en la ventana de SELECCIÓN.

    Nunca mira el hold-out: esa es toda la diferencia entre un router y una elección
    retrospectiva.
    """
    elegido: dict[str, str] = {}
    for cohorte in sorted(set(cohorts.values())):
        miembros = [k for k, c in cohorts.items() if c == cohorte]
        medias: dict[str, float] = {}
        for modelo in candidates:
            vals = [
                grid[(modelo, p, c)]["selection"][h]
                for (p, c) in miembros
                if (modelo, p, c) in grid and h in grid[(modelo, p, c)]["selection"]
            ]
            if vals:
                medias[modelo] = float(np.mean(vals))
        if medias:
            elegido[cohorte] = min(sorted(medias), key=lambda m: medias[m])
    return RouterRecipe(table=table, horizon=h, by_cohort=elegido)


def router_series_mase(
    recipe: RouterRecipe,
    cohorts: Mapping[tuple[str, str], str],
    grid: dict[tuple[str, str, str], dict[str, dict[int, float]]],
) -> pd.Series:
    """MASE de HOLD-OUT por serie del router: cada serie puntuada por el modelo de SU cohorte."""
    fuera: dict[tuple[str, str], float] = {}
    for (país, categoría), cohorte in cohorts.items():
        modelo = recipe.by_cohort.get(cohorte)
        if modelo is None:
            continue
        v = grid.get((modelo, país, categoría), {}).get("holdout", {}).get(recipe.horizon)
        if v is not None and np.isfinite(v):
            fuera[(país, categoría)] = v
    return pd.Series(fuera, name=recipe.name, dtype="float64")


def _series_mase(
    model_name: str, table: str, series: list[tuple[str, str]], hmax: int
) -> dict[tuple[str, str], dict[int, float]]:
    """``{(country, category): {h: MASE}}`` de un modelo sobre las series (omite fallos)."""
    out: dict[tuple[str, str], dict[int, float]] = {}
    for country, category in series:
        try:
            out[(country, category)] = mase_by_horizon(model_name, country, category, table, hmax)
        except (ValueError, IndexError, KeyError) as exc:
            log.warning("horizonte %s/%s/%s/%s omitido (%s)", model_name, country, category, table, exc)
    return out


def significance_by_horizon(
    table: str, champion: str = "drift", baseline: str = "naive1", hmax: int | None = None
) -> pd.DataFrame:
    """¿El campeón le gana al baseline SIGNIFICATIVAMENTE por horizonte?

    Test PAREADO por serie (cada serie evaluable = una observación, lo que absorbe la
    autocorrelación intra-serie de los orígenes solapados): Wilcoxon signed-rank sobre el
    MASE por serie ``champion`` vs ``baseline`` a cada horizonte, con corrección de Holm
    sobre los horizontes (misma maquinaria que ``champion.evaluate``). Un horizonte es
    ``sig`` si Holm rechaza Y el MASE medio del campeón es menor. Reconcilia el hallazgo
    del showdown deep GPU (rolling) contra el rigor canónico antes de tocar el ``.tex``.
    """
    hmax = hmax or max(HORIZONS)
    series = evaluable(table)
    ch = _series_mase(champion, table, series, hmax)
    ba = _series_mase(baseline, table, series, hmax)
    rows: list[dict] = []
    pvals: dict[str, float] = {}  # holm() llavea por str
    for h in HORIZONS:
        pairs = [(ch[k][h], ba[k][h]) for k in ch if k in ba and h in ch[k] and h in ba[k]]
        if len(pairs) < 6:  # muy pocas series para un test con sentido
            continue
        c = np.array([p[0] for p in pairs])
        b = np.array([p[1] for p in pairs])
        try:
            p = float(wilcoxon(c, b).pvalue)  # bilateral; la dirección la fija la media
        except ValueError:  # todos los pares idénticos
            p = 1.0
        pvals[str(h)] = p
        rows.append(
            {
                "h": h,
                champion: round(float(c.mean()), 3),
                baseline: round(float(b.mean()), 3),
                "delta_pct": round(float((b.mean() - c.mean()) / b.mean() * 100), 1),
                "wilcoxon_p": round(p, 5),
                "n": len(pairs),
            }
        )
    adj = significance.holm(pvals, alpha=0.05)
    df = pd.DataFrame(rows).set_index("h")
    df["holm_p"] = [round(float(adj[str(h)][0]), 5) for h in df.index]
    df["sig"] = [bool(adj[str(h)][1] and df.loc[h, champion] < df.loc[h, baseline]) for h in df.index]
    return df


def demo() -> None:
    """Self-check: MASE-por-horizonte de theta vs naive1 sobre MX/F3/FAD, crece con h."""
    for name in ("naive1", "theta"):
        mh = mase_by_horizon(name, "mexico", "F3", "FAD", max(HORIZONS))
        assert mh, f"{name}: sin MASE"
        assert mh[1] > 0 and mh[max(mh)] > mh[1], mh  # el error crece con el horizonte
        print(f"{name:7s} MASE por h: " + " ".join(f"h{h}={mh[h]:.3f}" for h in HORIZONS if h in mh))
    print("OK — walk-forward multi-horizonte rolling, F-only, escala canónica")


if __name__ == "__main__":
    demo()
