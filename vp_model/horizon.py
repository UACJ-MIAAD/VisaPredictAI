"""Campeón POR HORIZONTE — walk-forward multi-horizonte con orígenes RODANTES.

Corrige el sesgo época×horizonte del backtest de ventana fija que la auditoría del
showdown deep GPU (jul-2026, memoria ``project_gpu_multihorizon_showdown``) destapó:
``darts.historical_forecasts`` con ``forecast_horizon>1`` y ``last_points_only=False``
produce, en CADA origen (ventana expansible desde ``MIN_TRAIN``), un pronóstico de
h=1..H pasos; el paso k alimenta el horizonte h=k. Al rodar los orígenes por TODO el
span (no solo el hold-out de 24 meses) el horizonte queda desacoplado de la época.

Se puntúa SOLO sobre fechas F reales (mismo contrato que ``walkforward``) con la MISMA
escala MASE canónica (:func:`metrics.naive_scale_before`, naïve estacional train-before).
El campeón se elige por horizonte. Verdad medida (F-only, insesgado): a h=1 el random
walk (``naive1``) es piso; de h≈6-12 en adelante la parsimonia (Theta) lo bate ~13-35%.

Alcance: modelos CLÁSICOS (:data:`config.HORIZON_CANDIDATES`). El frontier deep no
aportó skill honesto (misma memoria); además los clásicos reentrenan en cada origen
(rolling verdadero) a bajo costo, mientras las redes están fijadas a ``forecast_horizon=1``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vp_model import dataset, metrics, models
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
    ts = fe.to_timeseries(dataset.load_series(country, category, table))
    min_train = MIN_TRAIN[table]
    if len(ts) < min_train + hmax:
        raise ValueError(f"serie corta ({len(ts)}) para min_train={min_train}+hmax={hmax}")
    model = models.build_model(model_name, table=table, block=_block(category))
    retrain: bool | int = model_name in RETRAIN_EACH_STEP
    cov = fe.covariates(ts)
    extra: dict[str, object] = {"future_covariates": cov} if cov is not None else {}
    per_origin = model.historical_forecasts(
        ts,
        start=min_train,
        forecast_horizon=hmax,
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


def mase_by_horizon(model_name: str, country: str, category: str, table: str, hmax: int) -> dict[int, float]:
    """``{h: MASE}`` F-only de un modelo sobre una serie, escala canónica train-before.

    La escala del MASE es la ÚNICA fuente del proyecto: naïve estacional in-sample
    sobre la serie F cruda anterior al hold-out (idéntica a ``walkforward.backtest``),
    de modo que los MASE por horizonte son comparables con las cifras canónicas.
    """
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
        rows.append(row)
    df = pd.DataFrame(rows).set_index("h")
    df["champion"] = df[list(candidates)].idxmin(axis=1)
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
