"""Caracterización feature-based de cada serie (EDA de panel, FPP3 cap. 4).

Convierte el panel de 194 series en una tabla analizable: un vector de
características por serie con fuerza de tendencia y estacionalidad, estructura de
autocorrelación, entropía espectral, estabilidad, orden de diferenciación,
outliers, prueba de ruido blanco y forma de la distribución.

Referencias (documentales):
  - Hyndman & Athanasopoulos, *Forecasting: Principles and Practice* 3.ª ed.
    (FPP3), cap. 3 (descomposición y fuerzas) y cap. 4 (features de series).
  - Cleveland, Cleveland, McRae & Terpenning, *STL* (1990).
  - Wang, Smith & Hyndman, "Characteristic-based clustering for time series" (2006).
"""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.signal import periodogram
from scipy.stats import kurtosis, skew
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.seasonal import STL
from statsmodels.tsa.stattools import acf, kpss

from vp_model import dataset, preprocess
from vp_model.config import SEASONAL_PERIOD

OUTLIER_Z = 3.0  # |z| del residuo STL para marcar outlier
LJUNG_BOX_LAGS = 2 * SEASONAL_PERIOD


@dataclass(frozen=True)
class SeriesFeatures:
    country: str
    category: str
    table: str
    trend_strength: float  # F_T in [0,1]: 1 = tendencia domina (FPP3 §3.6)
    seasonal_strength: float  # F_S in [0,1]: 1 = estacionalidad fuerte
    acf1: float  # autocorrelación lag-1 del nivel
    acf1_diff: float  # autocorrelación lag-1 de la serie diferenciada
    spectral_entropy: float  # [0,1]: 0 = muy predecible, 1 = ruido blanco
    stability: float  # varianza de las medias por bloque (no estacionariedad)
    lumpiness: float  # varianza de las varianzas por bloque (heteroscedasticidad)
    ndiffs: int  # diferenciaciones para estacionariedad (KPSS, FPP3 §9.1)
    n_outliers: int  # # de residuos STL con |z| > 3
    ljung_box_p: float  # p-valor H0: ruido blanco (sin autocorrelación)
    step_skew: float  # asimetría de los avances mensuales
    step_kurtosis: float  # curtosis (colas; retrogresiones extremas)


def _clean(country: str, category: str, table: str) -> pd.Series:
    """Serie mensual continua sin NaN, apta para STL/espectro (solo para EDA).

    Rellena los huecos largos por interpolación bidireccional: aceptable para
    caracterizar, NO para entrenar (el modelado los respeta vía preprocess).
    """
    s = preprocess.to_regular_monthly(dataset.load_series(country, category, table))
    return s.interpolate(limit_direction="both").astype("float64")


def stl_strengths(s: pd.Series, period: int = SEASONAL_PERIOD) -> tuple[float, float]:
    """Fuerza de tendencia y estacionalidad a partir de STL (FPP3 §3.6).

    F_T = max(0, 1 - Var(R)/Var(T+R)),  F_S = max(0, 1 - Var(R)/Var(S+R)).
    """
    res = STL(s, period=period, robust=True).fit()
    r = res.resid.to_numpy()
    t = res.trend.to_numpy()
    seas = res.seasonal.to_numpy()
    var_r = np.var(r)
    ft = max(0.0, 1.0 - var_r / np.var(t + r)) if np.var(t + r) > 0 else 0.0
    fs = max(0.0, 1.0 - var_r / np.var(seas + r)) if np.var(seas + r) > 0 else 0.0
    return float(ft), float(fs)


def spectral_entropy(s: pd.Series) -> float:
    """Entropía de Shannon normalizada del periodograma (Wang et al. 2006).

    Mide qué tan 'plano' es el espectro: cerca de 0 = señal concentrada y
    predecible; cerca de 1 = ruido blanco sin estructura.
    """
    _, psd = periodogram(s.to_numpy() - s.mean())
    psd = psd[psd > 0]
    if psd.size <= 1:
        return 1.0
    p = psd / psd.sum()
    return float(-np.sum(p * np.log(p)) / np.log(p.size))


def _tiled(s: pd.Series, width: int = SEASONAL_PERIOD) -> tuple[float, float]:
    """Estabilidad (var de medias por bloque) y lumpiness (var de varianzas)."""
    arr = s.to_numpy()
    n_tiles = len(arr) // width
    if n_tiles < 2:
        return 0.0, 0.0
    tiles = arr[: n_tiles * width].reshape(n_tiles, width)
    return float(np.var(tiles.mean(axis=1))), float(np.var(tiles.var(axis=1)))


def ndiffs(s: pd.Series, alpha: float = 0.05, max_d: int = 2) -> int:
    """# de diferenciaciones hasta que KPSS deja de rechazar estacionariedad (FPP3 §9.1)."""
    d = 0
    cur = s.copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        while d < max_d:
            try:
                p = kpss(cur.dropna(), regression="c", nlags="auto")[1]
            except ValueError, OverflowError:
                break
            if p >= alpha:  # no rechaza estacionariedad -> suficiente
                break
            cur = cur.diff().dropna()
            d += 1
    return d


def ljung_box_pvalue(s: pd.Series, lags: int = LJUNG_BOX_LAGS) -> float:
    """Prueba de Ljung-Box (FPP3 §2.9). H0: la serie es ruido blanco."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        lb = acorr_ljungbox(s.dropna(), lags=[min(lags, len(s) // 2)], return_df=True)
    return float(lb["lb_pvalue"].iloc[0])


def count_outliers(s: pd.Series, period: int = SEASONAL_PERIOD, z: float = OUTLIER_Z) -> int:
    """# de outliers = residuos STL con |z-score robusto| > z."""
    resid = STL(s, period=period, robust=True).fit().resid.to_numpy()
    med = np.median(resid)
    mad = np.median(np.abs(resid - med)) or 1.0
    zscore = 0.6745 * (resid - med) / mad  # z robusto (Iglewicz-Hoaglin)
    return int(np.sum(np.abs(zscore) > z))


def features(country: str, category: str, table: str) -> SeriesFeatures:
    """Vector completo de características de una serie."""
    s = _clean(country, category, table)
    diffs = s.diff().dropna()
    ft, fs = stl_strengths(s)
    stab, lump = _tiled(s)
    acf_level = acf(s, nlags=1, fft=True)[1]
    acf_diff = acf(diffs, nlags=1, fft=True)[1] if len(diffs) > 1 else 0.0
    return SeriesFeatures(
        country=country,
        category=category,
        table=table,
        trend_strength=round(ft, 4),
        seasonal_strength=round(fs, 4),
        acf1=round(float(acf_level), 4),
        acf1_diff=round(float(acf_diff), 4),
        spectral_entropy=round(spectral_entropy(s), 4),
        stability=round(stab, 2),
        lumpiness=round(lump, 2),
        ndiffs=ndiffs(s),
        n_outliers=count_outliers(s),
        ljung_box_p=round(ljung_box_pvalue(s), 4),
        step_skew=round(float(skew(diffs)), 3) if len(diffs) > 2 else 0.0,
        step_kurtosis=round(float(kurtosis(diffs)), 3) if len(diffs) > 2 else 0.0,
    )


def feature_table(table: str | None = None, block: str | None = None) -> pd.DataFrame:
    """Tabla de características para todas las series piloto (el EDA de panel)."""
    cat = dataset.list_series(table=table, block=block)
    return pd.DataFrame([asdict(features(r.country, r.category, r.table)) for r in cat.itertuples()])


# ---------------------------------------------------------------------------
# Diagnósticos avanzados (estado del arte): cambios de régimen, anomalías
# puntuales, memoria larga, complejidad, transformación de varianza, quiebre.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdvancedFeatures:
    country: str
    category: str
    table: str
    n_changepoints: int  # cambios de régimen (PELT, ruptures) — NO anomalías puntuales
    n_point_anomalies: int  # anomalías puntuales reales (filtro de Hampel sobre el residuo STL)
    hurst: float  # exponente de Hurst (DFA): >0.5 persistente/tendencia, <0.5 anti-persistente
    perm_entropy: float  # entropía de permutación normalizada (complejidad ordinal)
    sample_entropy: float  # entropía de muestra (regularidad/predecibilidad)
    boxcox_lambda: float  # lambda óptima de Box-Cox (estabilización de varianza; 1=sin transformar)
    za_break_pvalue: float  # Zivot-Andrews: raíz unitaria permitiendo UN quiebre estructural endógeno
    bds_pvalue: (
        float  # BDS (Brock et al.): H0 = i.i.d.; p bajo => dependencia no lineal (justifica modelos no lineales)
    )


def n_changepoints(s: pd.Series, pen_scale: float = 3.0) -> int:
    """Cambios de régimen vía PELT (Killick et al. 2012; ruptures).

    Detecta desplazamientos estructurales en media/varianza sobre la serie
    estandarizada. Esto separa los \textit{cambios de régimen} (pocos, duraderos) de
    las \textit{anomalías puntuales}, distinción que un z-score por punto confunde.
    """
    import ruptures as rpt

    x = ((s - s.mean()) / (s.std(ddof=0) or 1.0)).to_numpy()
    pen = pen_scale * np.log(len(x))  # penalización tipo BIC
    bkps = rpt.Pelt(model="rbf", min_size=6).fit(x).predict(pen=pen)
    return max(0, len(bkps) - 1)  # excluye el límite final


def n_point_anomalies(s: pd.Series, period: int = SEASONAL_PERIOD, window: int = 13, n_sigma: float = 3.0) -> int:
    """Anomalías puntuales por filtro de Hampel sobre el residuo STL (S-H-ESD-style).

    Sobre el residuo de la STL (ya sin tendencia ni estacionalidad), una ventana móvil
    marca como anómalo el punto cuyo desvío respecto a la mediana local supera
    ``n_sigma`` veces la MAD local. Robusto y local: no confunde un cambio de nivel
    sostenido con una ráfaga de outliers.
    """
    resid = pd.Series(STL(s, period=period, robust=True).fit().resid)
    med = resid.rolling(window, center=True, min_periods=window // 2).median()
    mad = (resid - med).abs().rolling(window, center=True, min_periods=window // 2).median()
    scale = 1.4826 * mad  # MAD -> sigma gaussiano
    return int((((resid - med).abs() > n_sigma * scale) & (scale > 0)).sum())


def advanced(country: str, category: str, table: str) -> AdvancedFeatures:
    """Vector de diagnósticos avanzados de una serie."""
    import antropy as ant
    from scipy import stats as sps
    from statsmodels.tsa.stattools import bds, zivot_andrews

    s = _clean(country, category, table)
    x = s.to_numpy()
    try:
        lam = float(sps.boxcox_normmax(x - x.min() + 1.0, method="mle"))
    except ValueError, OverflowError:
        lam = 1.0
    diff = s.diff().dropna()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            za_p = float(zivot_andrews(diff, trim=0.15)[1])
        except ValueError, np.linalg.LinAlgError:
            za_p = float("nan")
        try:
            # BDS sobre la serie diferenciada: H0 i.i.d.; p bajo = dependencia no lineal.
            bds_p = float(np.asarray(bds(diff, max_dim=2)[1]).ravel()[0])
        except ValueError, np.linalg.LinAlgError:
            bds_p = float("nan")
    return AdvancedFeatures(
        country=country,
        category=category,
        table=table,
        n_changepoints=n_changepoints(s),
        n_point_anomalies=n_point_anomalies(s),
        hurst=round(float(ant.detrended_fluctuation(x)), 4),
        perm_entropy=round(float(ant.perm_entropy(x, normalize=True)), 4),
        sample_entropy=round(float(ant.sample_entropy(x)), 4),
        boxcox_lambda=round(lam, 3),
        za_break_pvalue=round(za_p, 4) if not np.isnan(za_p) else float("nan"),
        bds_pvalue=round(bds_p, 4) if not np.isnan(bds_p) else float("nan"),
    )


def catch22_vector(country: str, category: str, table: str, catch24: bool = True) -> dict[str, float]:
    """Las 22 (o 24) características canónicas catch22 (Lubba et al. 2019, arXiv:1901.10200).

    Subconjunto de alto rendimiento de las 7000+ de \textit{hctsa}, seleccionado sobre
    93 problemas de clasificación. catch24 añade media y desviación estándar.
    """
    import pycatch22

    res = pycatch22.catch22_all(_clean(country, category, table).tolist(), catch24=catch24)
    return dict(zip(res["names"], res["values"], strict=True))


def advanced_table(table: str | None = None, block: str | None = None) -> pd.DataFrame:
    cat = dataset.list_series(table=table, block=block)
    return pd.DataFrame([asdict(advanced(r.country, r.category, r.table)) for r in cat.itertuples()])


# Contrato de datos del "feature store" (MLOps): rangos/tipos válidos por columna.
# Hecho a mano en vez de pandera para no añadir dependencia por lo que 20 líneas hacen.
def validate_feature_table(df: pd.DataFrame) -> None:
    """Valida invariantes del catálogo de features; lanza AssertionError si se violan."""
    assert not df.empty, "tabla de features vacía"
    unit = {"trend_strength", "seasonal_strength", "spectral_entropy"}
    for col in unit & set(df.columns):
        assert df[col].between(0, 1).all(), f"{col} fuera de [0,1]"
    if "ndiffs" in df:
        assert df["ndiffs"].between(0, 2).all(), "ndiffs fuera de [0,2]"
    if "ljung_box_p" in df:
        assert df["ljung_box_p"].between(0, 1).all(), "p-valor fuera de [0,1]"
    if "n_outliers" in df:
        assert (df["n_outliers"] >= 0).all(), "conteo negativo"
    assert not df.select_dtypes("number").isna().all(axis=0).any(), "columna numérica enteramente NaN"


def demo() -> None:
    """Self-check: las features tienen rangos válidos y discriminan series."""
    f = features("mexico", "F3", "FAD")
    assert 0.0 <= f.trend_strength <= 1.0 and 0.0 <= f.seasonal_strength <= 1.0
    assert 0.0 <= f.spectral_entropy <= 1.0
    assert f.trend_strength > 0.5, "una serie de fechas debe tener tendencia clara"
    assert f.seasonal_strength < 0.3, "las fechas de visa casi no tienen estacionalidad anual"
    assert f.ndiffs >= 1, "serie no estacionaria -> requiere diferenciar"
    assert f.ljung_box_p < 0.05, "una serie con tendencia NO es ruido blanco"
    print(
        f"OK — MX/F3/FAD features: F_T={f.trend_strength} F_S={f.seasonal_strength} "
        f"entropía={f.spectral_entropy} ndiffs={f.ndiffs} outliers={f.n_outliers} "
        f"LB_p={f.ljung_box_p} skew={f.step_skew}"
    )


if __name__ == "__main__":
    demo()
