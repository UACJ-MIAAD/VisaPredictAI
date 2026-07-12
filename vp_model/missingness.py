"""Manejo de valores faltantes al estado del arte (MNAR).

En este panel los huecos NO son aleatorios: corresponden a meses en régimen
\textit{Current}/\textit{Unavailable}, donde simplemente no hay fecha que predecir.
Es un mecanismo \textbf{Missing Not At Random} (MNAR): la ausencia misma es señal.
La interpolación ingenua es semánticamente incorrecta para el objetivo. Por eso:

  * Para CARACTERIZAR/visualizar (EDA): se imputa con suavizado de Kalman sobre un
    modelo de espacio de estados (equivalente a ``imputeTS::na_kalman``, el método más
    efectivo en los estudios comparativos), nunca interpolación lineal a ciegas.
  * Para MODELAR: enfoque \textit{imputation-free} — una máscara de observación y el
    tiempo-desde-la-última-observación como covariables, en vez de inventar valores
    (evita propagar el error de imputación al pronóstico).

Referencias: Moritz & Bartz-Beielstein, ``imputeTS'' (2017); literatura de modelado
imputation-free con máscaras y time-since-observation para series irregulares (2024-25).
"""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from vp_model import dataset


@dataclass(frozen=True)
class MissingnessProfile:
    n_months: int
    n_observed: int
    n_missing: int
    pct_missing: float
    n_gap_runs: int  # número de corridas de huecos (no meses sueltos)
    max_gap_run: int  # corrida de huecos más larga (meses)
    median_gap_run: float  # longitud mediana de las corridas
    longest_observed_run: int


def _raw_monthly(series: pd.Series) -> pd.Series:
    """Rejilla mensual continua SIN interpolar (los huecos quedan como NaN reales).

    A diferencia de ``preprocess.to_regular_monthly`` (que interpola huecos cortos para
    el modelado), aquí preservamos la ausencia para poder caracterizarla.
    """
    full = pd.date_range(series.index.min(), series.index.max(), freq="MS")
    return series.reindex(full).astype("float64")


def _gap_runs(missing: np.ndarray) -> list[int]:
    """Longitudes de las corridas consecutivas de True (faltante)."""
    runs, cur = [], 0
    for m in missing:
        if m:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


def profile(country: str, category: str, table: str) -> MissingnessProfile:
    """Caracteriza el patrón de ausencia (no solo cuántos faltan, sino cómo)."""
    s = _raw_monthly(dataset.load_series(country, category, table))
    missing = s.isna().to_numpy()
    gap_runs = _gap_runs(missing)
    obs_runs = _gap_runs(~missing)
    return MissingnessProfile(
        n_months=len(s),
        n_observed=int((~missing).sum()),
        n_missing=int(missing.sum()),
        pct_missing=round(float(missing.mean()), 4),
        n_gap_runs=len(gap_runs),
        max_gap_run=max(gap_runs) if gap_runs else 0,
        median_gap_run=float(np.median(gap_runs)) if gap_runs else 0.0,
        longest_observed_run=max(obs_runs) if obs_runs else 0,
    )


def kalman_impute(series: pd.Series) -> pd.Series:
    """Imputación por suavizado de Kalman (modelo de espacio de estados) para EDA.

    Ajusta un modelo estructural de tendencia local y rellena los faltantes con la
    media suavizada; es el método de referencia de ``imputeTS::na_kalman``. SOLO para
    caracterización/figuras: el modelado usa máscaras, no valores imputados.
    """
    from statsmodels.tsa.statespace.structural import UnobservedComponents

    reg = _raw_monthly(series)
    if reg.notna().sum() < 10 or reg.isna().sum() == 0:
        return reg.interpolate(limit_direction="both")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = UnobservedComponents(reg, level="local linear trend")
        res = model.fit(disp=False)
        smoothed = res.smoothed_state[0]  # nivel suavizado
    out = reg.copy()
    out[reg.isna()] = pd.Series(smoothed, index=reg.index)[reg.isna()]
    return out


def masking_features(series: pd.Series, horizon: int = 0) -> pd.DataFrame:
    """Covariables imputation-free para modelar series con huecos MNAR.

    Devuelve, sobre el calendario mensual regular:
      * ``observed``: 1 si el mes tiene fecha (estado F), 0 si no.
      * ``months_since_obs``: meses transcurridos desde la última observación.
    Estas dos columnas dejan que el modelo aprenda del patrón de ausencia en lugar de
    confiar en valores inventados (Che et al. GRU-D; literatura imputation-free).

    Ambas son CAUSALES: el valor del mes m depende solo de la observabilidad de los
    meses ≤ m (US-F1: entran al catálogo como covariables con retardo −1, el último
    mes CERRADO conocido en el origen; el régimen del mes objetivo no se conoce antes
    de publicarse el boletín y filtrarlo sería fuga).

    ``horizon`` (>0) extiende la rejilla ese número de meses hacia el futuro con el
    estado de información honesto al momento de pronosticar: ``observed=0`` y el
    contador siguiendo su cuenta (esos meses aún no se publican). Lo usan los caminos
    que predicen más allá del último boletín (``predict(n)``).
    """
    reg = _raw_monthly(series)
    if horizon > 0:
        ext = pd.date_range(reg.index[-1] + pd.offsets.MonthBegin(1), periods=horizon, freq="MS")
        reg = reg.reindex(reg.index.append(ext))
    observed = (~reg.isna()).astype("int64")
    since = np.zeros(len(reg), dtype="int64")
    count = 0
    for i, obs in enumerate(observed.to_numpy()):
        count = 0 if obs else count + 1
        since[i] = count
    return pd.DataFrame({"observed": observed, "months_since_obs": since}, index=reg.index)


def profile_all(table: str | None = None, block: str | None = None) -> pd.DataFrame:
    cat = dataset.list_series(table=table, block=block)
    return pd.DataFrame(
        [
            {
                "country": r.country,
                "category": r.category,
                "table": r.table,
                **asdict(profile(r.country, r.category, r.table)),
            }
            for r in cat.itertuples()
        ]
    )


def demo() -> None:
    """Self-check: caracterización de huecos, Kalman e imputation-free coherentes.

    Ejemplar: mexico/EB4_RW/FAD (con huecos reales). El ejemplar previo (china/F1)
    quedó SIN huecos tras la resurrección I1 (2-jul-2026) y el demo fallaba — misma
    lección que la figura Kalman (verificar con ``missingness.profile`` al elegir).
    """
    p = profile("mexico", "EB4_RW", "FAD")
    assert p.n_missing == p.n_months - p.n_observed
    assert p.n_gap_runs >= 1 and p.max_gap_run >= 1

    raw = dataset.load_series("mexico", "EB4_RW", "FAD")
    imp = kalman_impute(raw)
    assert imp.isna().sum() == 0, "Kalman debe rellenar todos los huecos para EDA"
    # los valores observados no se alteran (se comparan contra la rejilla cruda)
    obs = _raw_monthly(raw)
    mask = obs.notna()
    assert np.allclose(imp[mask].to_numpy(), obs[mask].to_numpy(), rtol=1e-6)

    mf = masking_features(raw)
    assert set(mf.columns) == {"observed", "months_since_obs"}
    assert mf["months_since_obs"].max() == p.max_gap_run  # el contador llega al hueco mayor
    assert (mf.loc[mf["observed"] == 1, "months_since_obs"] == 0).all()
    print(
        f"OK — MX/EB4_RW/FAD MNAR: {p.pct_missing:.1%} faltante en {p.n_gap_runs} corridas "
        f"(máx {p.max_gap_run} m); Kalman rellena {p.n_missing}; máscara+Δt como covariables"
    )


if __name__ == "__main__":
    demo()
