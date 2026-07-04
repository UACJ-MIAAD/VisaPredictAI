"""Análisis exploratorio y caracterización de series (US-C1, US-C2).

Caracteriza cada serie y_{p,c,b,t}, aplica la cobertura escalonada del Anteproyecto
(estructural -> evaluable -> piloto) y corre pruebas de estructura temporal
(ADF/KPSS de estacionariedad). Las figuras publication-ready viven en
``vp_model.plots``; aquí está la parte numérica reproducible.
"""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, kpss

from vp_model import dataset
from vp_model.config import MIN_TRAINABLE_EVALUABLE


@dataclass(frozen=True)
class SeriesProfile:
    country: str
    category: str
    table: str
    n_obs: int
    span_months: int
    continuity: float  # fracción de meses con observación F sobre el tramo
    n_gaps: int  # # de meses sin F dentro del tramo
    n_retrogressions: int  # # de meses donde la fecha de prioridad retrocede
    median_step_days: float  # avance mensual mediano (días/mes)
    is_monotonic: bool
    evaluable: bool


def profile_series(country: str, category: str, table: str) -> SeriesProfile:
    """Caracteriza una serie: longitud, continuidad, retrogresiones, tendencia."""
    s = dataset.load_series(country, category, table)
    full = pd.date_range(s.index.min(), s.index.max(), freq="MS")
    span = len(full)
    diffs = s.diff().dropna()
    return SeriesProfile(
        country=country,
        category=category,
        table=table,
        n_obs=len(s),
        span_months=span,
        continuity=round(len(s) / span, 4),
        n_gaps=span - len(s),
        n_retrogressions=int((diffs < 0).sum()),
        median_step_days=float(diffs.median()) if len(diffs) else 0.0,
        is_monotonic=bool(s.is_monotonic_increasing),
        evaluable=len(s) >= MIN_TRAINABLE_EVALUABLE,
    )


def profile_all(table: str | None = None, block: str | None = None) -> pd.DataFrame:
    """Tabla de perfiles para todas las series piloto (matriz de elegibilidad)."""
    cat = dataset.list_series(table=table, block=block)
    rows = [asdict(profile_series(r.country, r.category, r.table)) for r in cat.itertuples()]
    return pd.DataFrame(rows)


def stationarity(country: str, category: str, table: str) -> dict[str, float | bool | str]:
    """ADF + KPSS sobre una serie (US-C2).

    ADF H0: tiene raíz unitaria (no estacionaria). KPSS H0: es estacionaria (en
    nivel). Sus H0 son opuestas, por eso se reportan juntas: el acuerdo entre ambas
    da el diagnóstico robusto que decide la diferenciación.
    """
    s = dataset.load_series(country, category, table).astype("float64")
    with warnings.catch_warnings():
        # KPSS satura el p-valor fuera de [0.01, 0.10]; el InterpolationWarning es esperado.
        warnings.simplefilter("ignore")
        adf_p = float(adfuller(s, autolag="AIC")[1])
        kpss_p = float(kpss(s, regression="c", nlags="auto")[1])
        # DF-GLS (Elliott-Rothenberg-Stock): detrending GLS local-to-unity; domina a ADF
        # en POTENCIA justo bajo tendencia fuerte + muestra corta, donde ADF colapsa.
        try:
            from arch.unitroot import DFGLS

            dfgls_p = float(DFGLS(s.to_numpy(), trend="ct").pvalue)
        except ImportError, ValueError:
            dfgls_p = float("nan")
    adf_stationary = adf_p < 0.05  # rechaza raíz unitaria
    kpss_stationary = kpss_p >= 0.05  # no rechaza estacionariedad
    return {
        "adf_pvalue": round(adf_p, 4),
        "kpss_pvalue": round(kpss_p, 4),
        "dfgls_pvalue": round(dfgls_p, 4) if not np.isnan(dfgls_p) else float("nan"),
        "adf_stationary": adf_stationary,
        "kpss_stationary": kpss_stationary,
        # Acuerdo: ambas estacionarias -> nivel; ambas no -> diferenciar; mixto -> revisar.
        "verdict": _verdict(adf_stationary, kpss_stationary),
    }


def _verdict(adf_stationary: bool, kpss_stationary: bool) -> str:
    if adf_stationary and kpss_stationary:
        return "stationary"
    if not adf_stationary and not kpss_stationary:
        return "difference"
    return "mixed"


def demo() -> None:
    """Self-check: el perfil es coherente y la estacionariedad clasifica México FAD."""
    p = profile_series("mexico", "F3", "FAD")
    assert p.n_obs > 0 and 0 < p.continuity <= 1.0
    assert p.n_gaps == p.span_months - p.n_obs
    assert p.evaluable, "MX/F3/FAD debería ser evaluable (>=84 obs)"

    df = profile_all(table="FAD", block="family")
    assert len(df) == 25, f"esperaba 25 series FAD familiares piloto, hay {len(df)}"
    assert df["evaluable"].all(), "todas las FAD familiares piloto son largas"

    st = stationarity("mexico", "F3", "FAD")
    # Una serie de fechas que avanza ~monótona NO es estacionaria en nivel.
    assert st["verdict"] in {"difference", "mixed"}, st
    assert not np.isnan(st["adf_pvalue"])
    print(
        f"OK — MX/F3/FAD perfil: {p.n_obs} obs, cont {p.continuity:.0%}, "
        f"{p.n_retrogressions} retro, paso {p.median_step_days:.0f} d/mes; "
        f"ADF p={st['adf_pvalue']} KPSS p={st['kpss_pvalue']} -> {st['verdict']}"
    )


if __name__ == "__main__":
    demo()
