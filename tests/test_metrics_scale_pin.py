"""D2: la escala del MASE está ANCLADA — m=12 (estacional) y m=1 (RW) no cambian callados.

Characterization con valores exactos calculados a mano sobre series sintéticas: si
alguien toca SEASONAL_PERIOD, la fórmula del denominador o el corte por fecha de
naive_scale_before, estos asserts truenan con el delta visible. Toda cifra MASE
publicada (tesis/paper/web/scorecards) depende de esta convención.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pandas")

from vp_model.config import SEASONAL_PERIOD  # noqa: E402
from vp_model.metrics import naive_scale_before, seasonal_naive_mae  # noqa: E402


def test_seasonal_period_is_twelve() -> None:
    """El denominador estacional del MASE es m=12 (boletines mensuales)."""
    assert SEASONAL_PERIOD == 12


def test_seasonal_naive_mae_exact_values() -> None:
    # Serie 0..23: |y_t - y_{t-m}| = m para todo t>=m ⇒ MAE = m, para cualquier m.
    vals = np.arange(24, dtype=float)
    assert seasonal_naive_mae(vals, m=12) == 12.0
    assert seasonal_naive_mae(vals, m=1) == 1.0
    # Escalón: solo un salto de 10 en t=12 rompe la linealidad.
    step = np.array([0.0] * 12 + [10.0] * 12)
    assert seasonal_naive_mae(step, m=12) == 10.0  # cada par (t, t-12) difiere en 10
    assert seasonal_naive_mae(step, m=1) == pytest.approx(10.0 / 23)  # un solo salto entre 23 difs


def test_naive_scale_before_cuts_by_date_not_position() -> None:
    """Leakage-free: el CORTE es por fecha (solo pasado), y el denominador es
    posicional sobre lo que queda — ambas convenciones ancladas tal como SON."""
    idx = pd.date_range("2020-01-01", periods=36, freq="MS")
    s = pd.Series(np.arange(36, dtype=float), index=idx)
    cutoff = pd.Timestamp("2022-01-01")  # deja 24 puntos de train
    assert naive_scale_before(s, cutoff, m=12) == 12.0
    # Con huecos internos (6 meses caídos), los pares (t, t-12) son POSICIONALES sobre
    # el tramo post-corte: los valores quedan 18 aparte en la escala original. Este 18.0
    # es la convención vigente que produjo toda cifra publicada — cambiarla a alineación
    # por fecha sería un cambio numérico global que exige re-derivación y regla #0.
    gappy = s.drop(idx[10:16])
    assert naive_scale_before(gappy, cutoff, m=12) == 18.0
