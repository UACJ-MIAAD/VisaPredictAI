"""Preprocesamiento para modelado, sin fuga de información (US-C3).

Cubre lo que pide §1.2.2 del formato de seguimiento: manejo de huecos,
diferenciación y construcción de regresores de calendario. El escalado vive en
``feature_builder``/walkforward (darts Scaler ajustado SOLO en la ventana
inicial); toda decisión que toque estadísticos del conjunto se ajusta sobre el
tramo de entrenamiento — ajustarla sobre el total filtraría el futuro al pasado.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Huecos cortos (meses C/U intercalados) se interpolan; los largos se dejan como
# NaN para que el modelo no invente una rampa lineal sobre años sin dato.
from vp_model.config import MAX_INTERPOLABLE_GAP


def to_regular_monthly(series: pd.Series, max_gap: int = MAX_INTERPOLABLE_GAP) -> pd.Series:
    """Reindexa a frecuencia mensual continua e interpola solo huecos cortos.

    ⚠️ SOLO para caracterización/EDA/figuras (uso retrospectivo sobre la serie
    completa). La interpolación lineal es BIDIRECCIONAL: el valor de un mes de
    hueco usa el bracket observado FUTURO, así que esta rejilla NO puede alimentar
    el entrenamiento por origen del walk-forward (fuga temporal, US-F1). La capa
    de modelado usa :func:`to_regular_monthly_causal`.

    Criterio documentado: interpolar linealmente corridas de NaN de hasta
    ``max_gap`` meses; dejar las corridas más largas como NaN (decisión
    consciente, no imputación a ciegas).
    """
    full = pd.date_range(series.index.min(), series.index.max(), freq="MS")
    s = series.reindex(full).astype("float64")
    s.index.name = "month"
    filled = s.interpolate(method="linear", limit_area="inside")
    # Todo-o-nada por corrida: revierte a NaN las corridas de hueco con largo > max_gap
    # (no dejar una rampa parcial sobre años sin dato).
    isna = s.isna().to_numpy()
    run_id = (~isna).cumsum()
    run_len = pd.Series(isna).groupby(run_id).transform("sum").to_numpy()
    keep = ~isna | (run_len <= max_gap)
    return filled.where(keep, other=np.nan)


def to_regular_monthly_causal(series: pd.Series) -> pd.Series:
    """Rejilla mensual regular con relleno CAUSAL (forward-only, US-F1).

    Política elegida (F1): todo mes de hueco se rellena con la ÚLTIMA observación
    anterior (*last observation carried forward*, LOCF), sin tope. El valor
    asignado al mes m depende SOLO de observaciones ≤ m, de modo que UNA sola
    serie transformada es válida para TODOS los orígenes del walk-forward: mutar
    cualquier valor posterior a un origen no puede cambiar ningún insumo de
    entrenamiento en/antes de ese origen (propiedad metamórfica probada en
    ``tests/test_temporal_leakage.py``). La interpolación lineal bidireccional
    previa usaba el bracket futuro del hueco y filtraba el futuro al pasado para
    los orígenes dentro del hueco. Los meses rellenados existen solo para dar
    continuidad al entrenamiento: NUNCA se puntúan (máscara F-only, B1) y los
    modelos capaces de covariables reciben además las máscaras MNAR
    (``missingness.masking_features``) para descontar el arrastre; el resto del
    catálogo usa esta política forward-only explícita.
    """
    full = pd.date_range(series.index.min(), series.index.max(), freq="MS")
    s = series.reindex(full).astype("float64")
    s.index.name = "month"
    # El primer punto de la rejilla es una observación real (la rejilla arranca en
    # series.index.min()), así que el ffill no deja NaN a la cabeza.
    return s.ffill()


# AD7: la clase Standardizer (z-score fit/transform propio) se ELIMINÓ — era una
# segunda abstracción de escalado que ninguna ruta de producción usaba; el camino
# real es darts Scaler ajustado solo en la ventana inicial (walkforward, ya
# leakage-free). Una sola abstracción viva, no dos.


def difference(series: pd.Series) -> pd.Series:
    """Primera diferencia Δy. Contraparte exacta de ``undifference`` (AD2).

    ÚNICA implementación pandas del transform más importante del proyecto; los
    caminos deep (run_global_deep) la importan en vez de re-tipear ``.diff()``
    con su propia semántica de NaN. El wrapper darts (models.Differenced) usa
    ``TimeSeries.diff()`` con la MISMA semántica y reintegra con el mismo
    contrato causal (anclar al último nivel observado).
    """
    return series.diff()


def undifference(deltas: pd.Series, last_level: float) -> pd.Series:
    """Reintegra deltas al nivel: cumsum anclado al último nivel OBSERVADO (causal)."""
    return last_level + deltas.cumsum()


def calendar_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Regresores exógenos de calendario para modelos tabulares (XGBoost).

    El año fiscal de visas de EE.UU. arranca en octubre; las cuotas se reinician
    entonces, lo que mueve las fechas de prioridad. Codificamos el mes y la posición
    dentro del año fiscal de forma cíclica (seno/coseno) para no imponer un orden
    falso entre diciembre y enero.
    """
    month = index.month
    fiscal_pos = (month - 10) % 12  # 0 en octubre
    return pd.DataFrame(
        {
            "month_sin": np.sin(2 * np.pi * month / 12),
            "month_cos": np.cos(2 * np.pi * month / 12),
            "fiscal_sin": np.sin(2 * np.pi * fiscal_pos / 12),
            "fiscal_cos": np.cos(2 * np.pi * fiscal_pos / 12),
            "year": index.year,
        },
        index=index,
    )


def demo() -> None:
    """Self-check: interpolación acotada + relleno causal + round-trip de diferenciación."""
    from vp_model import dataset

    raw = dataset.load_series("mexico", "EB4_RW", "FAD")  # serie con huecos reales (post-I1)
    reg = to_regular_monthly(raw)
    assert reg.index.freq == "MS"
    # Los valores observados no cambian; solo se rellenan huecos cortos.
    assert np.allclose(reg.loc[raw.index].to_numpy(), raw.to_numpy())

    # Hueco artificial largo (> max_gap) debe quedar como NaN.
    s = pd.Series([0.0, np.nan, np.nan, np.nan, np.nan, 100.0], index=pd.date_range("2020-01-01", periods=6, freq="MS"))
    assert to_regular_monthly(s, max_gap=3).isna().sum() == 4

    # F1: el relleno causal es forward-only — el hueco toma el valor del bracket
    # IZQUIERDO (0.0), jamás una rampa hacia el bracket futuro (100.0).
    causal = to_regular_monthly_causal(s.dropna())
    assert causal.isna().sum() == 0
    assert (causal.iloc[1:5] == 0.0).all(), "LOCF: el hueco debe arrastrar la última observación"

    # Round-trip exacto de difference/undifference (contrato AD2).
    full = dataset.load_series("mexico", "F3", "FAD").astype("float64")
    d = difference(full)
    back = undifference(d.iloc[1:], last_level=float(full.iloc[0]))
    assert np.allclose(back.to_numpy(), full.iloc[1:].to_numpy())

    feats = calendar_features(full.index)
    assert list(feats.columns) == ["month_sin", "month_cos", "fiscal_sin", "fiscal_cos", "year"]
    print(
        f"OK — MX/EB4_RW/FAD regular {len(reg)} meses (LOCF causal verificado); "
        f"round-trip diff exacto; {feats.shape[1]} regresores de calendario"
    )


if __name__ == "__main__":
    demo()
