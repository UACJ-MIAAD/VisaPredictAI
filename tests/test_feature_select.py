"""Contrato de la selección/des-redundancia de features (brecha ALTA SOTA).

``feature_select`` solo usa numpy/scipy/statsmodels -> corre siempre.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vp_model import feature_select as fs


def _data(n: int = 200, seed: int = 0):
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=n)
    y = pd.Series(signal + rng.normal(0, 0.3, n))
    x = pd.DataFrame(
        {
            "relevante": signal,
            "relevante_dup": signal + rng.normal(0, 0.01, n),  # redundante con 'relevante'
            "ruido1": rng.normal(size=n),
            "ruido2": rng.normal(size=n),
        }
    )
    return x, y


def test_relevance_separates_signal_from_noise() -> None:
    x, y = _data()
    p = fs.relevance_pvalues(x, y)
    assert p["relevante"] < 0.05
    assert p["ruido1"] > 0.05 and p["ruido2"] > 0.05


def test_fdr_keeps_only_relevant() -> None:
    x, y = _data()
    rel = fs.fdr_relevant(fs.relevance_pvalues(x, y))
    assert "relevante" in rel
    assert "ruido1" not in rel and "ruido2" not in rel


def test_deredundant_collapses_correlated() -> None:
    x, _ = _data()
    kept, dropped = fs.deredundant(x, ["relevante", "relevante_dup"], threshold=0.9)
    assert kept == ["relevante"]
    assert dropped == {"relevante_dup": "relevante"}


def test_select_end_to_end() -> None:
    x, y = _data()
    sel = fs.select(x, y)
    assert sel.selected == ["relevante"]  # 1 relevante no redundante
    assert "relevante_dup" in sel.dropped_redundant


def test_constant_feature_is_irrelevant() -> None:
    x, y = _data()
    x = x.assign(constante=5.0)
    assert fs.relevance_pvalues(x, y)["constante"] == 1.0
