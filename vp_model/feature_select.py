"""Selección y des-redundancia de características (brecha ALTA del estado del arte).

La revisión de literatura concluyó que la mejora de mayor retorno no es extraer más
features sino SELECCIONARLAS bien: con n=125-290 observaciones por serie cada grado de
libertad cuenta. Se implementa el patrón de dos etapas:

  1. Relevancia (estilo FRESH, Christ et al. 2018): prueba de hipótesis univariada
     feature-vs-objetivo + corrección de multiplicidad de Benjamini-Yekutieli, que
     controla la tasa de falso descubrimiento (FDR) bajo dependencia entre features.
  2. Des-redundancia (estilo mRMR, Ding & Peng 2005): de cada grupo de features
     mutuamente correlacionadas se conserva una sola representante (la más relevante),
     resolviendo la colinealidad que FRESH por sí solo no atiende.

Diseño leakage-free: se ajusta SOLO con datos de entrenamiento (FRESH asume
intercambiabilidad entre instancias; en panel/walk-forward debe aplicarse intra-fold).
Para n pequeño se usa correlación de Spearman en vez de información mutua (estimación
ruidosa con pocas muestras).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import kendalltau
from statsmodels.stats.multitest import multipletests


@dataclass(frozen=True)
class Selection:
    relevant: list[str]  # features que pasan el control de FDR
    selected: list[str]  # relevantes y no redundantes (conjunto final)
    dropped_redundant: dict[str, str]  # feature descartada -> representante que la cubre


def relevance_pvalues(x: pd.DataFrame, y: pd.Series) -> pd.Series:
    """P-valor de relevancia univariada por feature (Kendall tau vs el objetivo).

    Kendall es robusto a no linealidad monótona y a muestras chicas; apto para el
    objetivo continuo del proyecto.
    """
    out = {}
    yv = y.to_numpy()
    for col in x.columns:
        xv = x[col].to_numpy()
        if np.nanstd(xv) == 0:
            out[col] = 1.0  # constante -> irrelevante
            continue
        _, p = kendalltau(xv, yv, nan_policy="omit")
        out[col] = 1.0 if np.isnan(p) else float(p)
    return pd.Series(out)


def fdr_relevant(pvalues: pd.Series, alpha: float = 0.05) -> list[str]:
    """Features cuyo p-valor sobrevive la corrección de Benjamini-Yekutieli (FDR)."""
    if pvalues.empty:
        return []
    keep, _, _, _ = multipletests(pvalues.to_numpy(), alpha=alpha, method="fdr_by")
    return [c for c, k in zip(pvalues.index, keep, strict=True) if k]


def deredundant(x: pd.DataFrame, ranking: list[str], threshold: float = 0.9) -> tuple[list[str], dict[str, str]]:
    """De cada grupo con |Spearman| > threshold conserva la feature mejor rankeada.

    ``ranking`` es el orden de preferencia (p.ej. por relevancia ascendente en p-valor);
    se recorre y se descarta toda feature muy correlacionada con una ya conservada.
    """
    if not ranking:
        return [], {}
    corr = x[ranking].corr(method="spearman").abs()
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for col in ranking:
        rep = next((k for k in kept if corr.loc[col, k] > threshold), None)
        if rep is None:
            kept.append(col)
        else:
            dropped[col] = rep
    return kept, dropped


def select(x: pd.DataFrame, y: pd.Series, *, alpha: float = 0.05, corr_threshold: float = 0.9) -> Selection:
    """Pipeline FRESH + des-redundancia: relevancia con FDR, luego anti-colinealidad."""
    num = x.select_dtypes("number")
    pvals = relevance_pvalues(num, y)
    relevant = fdr_relevant(pvals, alpha)
    ranking = pvals[relevant].sort_values().index.tolist()  # más relevante (p menor) primero
    selected, dropped = deredundant(num, ranking, corr_threshold)
    return Selection(relevant=relevant, selected=selected, dropped_redundant=dropped)


def demo() -> None:
    """Self-check: selecciona relevantes, descarta ruido y colapsa redundancia."""
    rng = np.random.default_rng(0)
    n = 200
    signal = rng.normal(size=n)
    y = pd.Series(signal + rng.normal(0, 0.3, n))
    x = pd.DataFrame(
        {
            "relevante": signal,
            "relevante_dup": signal + rng.normal(0, 0.01, n),  # casi idéntica -> redundante
            "ruido1": rng.normal(size=n),
            "ruido2": rng.normal(size=n),
        }
    )
    sel = select(x, y)
    assert "relevante" in sel.relevant, sel
    assert "ruido1" not in sel.relevant and "ruido2" not in sel.relevant
    # de las dos relevantes correlacionadas, solo una sobrevive
    assert ("relevante_dup" in sel.dropped_redundant) or ("relevante" in sel.dropped_redundant)
    assert len(sel.selected) == 1
    print(
        f"OK — selección: relevantes={sel.relevant} -> finales={sel.selected}; "
        f"redundantes descartadas={sel.dropped_redundant}"
    )


if __name__ == "__main__":
    demo()
