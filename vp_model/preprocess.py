"""Preprocesamiento para modelado, sin fuga de información (US-C3).

Cubre lo que pide §1.2.2 del formato de seguimiento: manejo de huecos, escalado y
construcción de regresores. Las decisiones que tocan estadísticos del conjunto
(escalado) se ajustan SOLO sobre el tramo de entrenamiento; ajustarlas sobre el
total filtraría el futuro al pasado (leakage). El test `demo()` lo verifica.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Huecos cortos (meses C/U intercalados) se interpolan; los largos se dejan como
# NaN para que el modelo no invente una rampa lineal sobre años sin dato.
from vp_model.config import MAX_INTERPOLABLE_GAP


def to_regular_monthly(series: pd.Series, max_gap: int = MAX_INTERPOLABLE_GAP) -> pd.Series:
    """Reindexa a frecuencia mensual continua e interpola solo huecos cortos.

    Los modelos de series de tiempo (ARIMA, darts) requieren un índice regular; el
    panel es disperso porque los meses C/U no son objetivo. Criterio documentado:
    interpolar linealmente corridas de NaN de hasta ``max_gap`` meses; dejar las
    corridas más largas como NaN (decisión consciente, no imputación a ciegas).
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


@dataclass(frozen=True)
class Standardizer:
    """Estandarización z = (x - mu) / sigma con estadísticos del TRAIN únicamente."""

    mean: float
    std: float

    @classmethod
    def fit(cls, train: pd.Series) -> Standardizer:
        std = float(train.std(ddof=0))
        return cls(mean=float(train.mean()), std=std if std > 0 else 1.0)

    def transform(self, x: pd.Series) -> pd.Series:
        return (x - self.mean) / self.std

    def inverse(self, z: pd.Series | np.ndarray) -> pd.Series | np.ndarray:
        return z * self.std + self.mean


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
    """Self-check: interpolación acotada + escalado sin leakage."""
    from vp_model import dataset

    raw = dataset.load_series("china", "F1", "FAD")  # tiene 5 huecos
    reg = to_regular_monthly(raw)
    assert reg.index.freq == "MS"
    # Los valores observados no cambian; solo se rellenan huecos cortos.
    assert np.allclose(reg.loc[raw.index].to_numpy(), raw.to_numpy())

    # Hueco artificial largo (> max_gap) debe quedar como NaN.
    s = pd.Series([0.0, np.nan, np.nan, np.nan, np.nan, 100.0], index=pd.date_range("2020-01-01", periods=6, freq="MS"))
    assert to_regular_monthly(s, max_gap=3).isna().sum() == 4

    # Leakage: el estandarizador ajustado en train NO conoce la media del total.
    full = dataset.load_series("mexico", "F3", "FAD").astype("float64")
    train, test = full.iloc[:-24], full.iloc[-24:]
    sc = Standardizer.fit(train)
    assert abs(sc.mean - train.mean()) < 1e-9
    assert abs(sc.mean - full.mean()) > 1.0, "la media de train != media del total (hay tendencia)"
    # round-trip exacto.
    z = sc.transform(test)
    assert np.allclose(np.asarray(sc.inverse(z)), test.to_numpy())

    feats = calendar_features(full.index)
    assert list(feats.columns) == ["month_sin", "month_cos", "fiscal_sin", "fiscal_cos", "year"]
    print(
        f"OK — CN/F1/FAD regular {len(reg)} meses; scaler train mu={sc.mean:.0f} "
        f"(total mu={full.mean():.0f}); {feats.shape[1]} regresores de calendario"
    )


if __name__ == "__main__":
    demo()
