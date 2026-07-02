"""Significancia estadística de las diferencias entre modelos (US-F3).

Suite de comparación, de lo pareado a lo global:
  * Diebold-Mariano (1995) + Holm: comparación pareada de exactitud (h=1).
  * Giacomini-White (2006): habilidad predictiva CONDICIONAL, diseñada para esquemas
    con ventana como el walk-forward; detecta superioridad bajo condiciones, no solo
    en promedio.
  * Model Confidence Set (Hansen, Lunde & Nason 2011): el subconjunto de modelos
    estadísticamente indistinguibles del mejor, con control del error de familia.
  * Friedman + Nemenyi (Demšar 2006): comparación de muchos modelos sobre muchas
    series mediante rangos, con diagrama de diferencia crítica.
Viven SOLO en la fase de evaluación (Cap IV §4.4), nunca en el cuerpo de la propuesta.
"""

from __future__ import annotations

from typing import TypedDict

import numpy as np
import pandas as pd
from scipy import stats


class FriedmanResult(TypedDict):
    friedman_stat: float
    friedman_p: float
    avg_rank: pd.Series
    nemenyi: pd.DataFrame


def dm_test(e1: np.ndarray, e2: np.ndarray, *, h: int = 1, power: int = 2) -> tuple[float, float]:
    """Diebold-Mariano: ¿difieren significativamente las exactitudes de dos modelos?

    e1, e2: errores de pronóstico alineados (mismo origen, misma serie). Devuelve
    (estadístico, p-valor bilateral). Para h=1 la varianza de largo plazo se reduce a
    la varianza muestral del diferencial de pérdidas. H0: igual exactitud.
    """
    e1, e2 = np.asarray(e1, dtype="float64"), np.asarray(e2, dtype="float64")
    if e1.shape != e2.shape:
        raise ValueError("las series de error deben tener la misma longitud")
    d = np.abs(e1) ** power - np.abs(e2) ** power
    n = len(d)
    dbar = d.mean()
    # Varianza de largo plazo: para h>1 suma autocovarianzas hasta h-1 (Newey-West simple).
    gamma0 = d.var(ddof=0)
    var = gamma0
    for lag in range(1, h):
        cov = np.cov(d[lag:], d[:-lag], ddof=0)[0, 1]
        var += 2 * cov
    if var <= 0:
        return 0.0, 1.0
    dm = dbar / np.sqrt(var / n)
    # Corrección de Harvey-Leybourne-Newbold por muestra finita.
    adj = np.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    dm *= adj
    pvalue = 2 * stats.t.cdf(-abs(dm), df=n - 1)
    return float(dm), float(pvalue)


def holm(pvalues: dict[str, float], alpha: float = 0.05) -> dict[str, tuple[float, bool]]:
    """Corrección de Holm-Bonferroni sobre una familia de p-valores.

    Devuelve por comparación (p_ajustado, rechaza_H0). Controla el FWER al nivel alpha
    sin asumir independencia, más potente que Bonferroni puro.
    """
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, tuple[float, bool]] = {}
    running_max = 0.0
    for i, (key, p) in enumerate(items):
        adj = min(1.0, (m - i) * p)
        running_max = max(running_max, adj)  # monotonicidad del ajuste de Holm
        out[key] = (running_max, running_max < alpha)
    return out


def giacomini_white(e1: np.ndarray, e2: np.ndarray, *, power: int = 2) -> tuple[float, float]:
    """Prueba de habilidad predictiva condicional de Giacomini-White (2006).

    Regresa el diferencial de pérdidas sobre una constante y su propio rezago
    (instrumentos); el estadístico de Wald ~ chi^2 con 2 g.l. prueba H0: igual
    habilidad condicional. Válida para esquemas de estimación con ventana (walk-forward),
    donde el Diebold-Mariano clásico puede ser inválido.
    """
    e1, e2 = np.asarray(e1, "float64"), np.asarray(e2, "float64")
    d = np.abs(e1) ** power - np.abs(e2) ** power
    d = d[~np.isnan(d)]
    n = len(d)
    if n < 5:
        return float("nan"), float("nan")
    # Instrumentos: constante + diferencial rezagado.
    h = np.column_stack([np.ones(n - 1), d[:-1]])
    dd = d[1:]
    reg = h * dd[:, None]  # h_t * d_{t+1}
    gbar = reg.mean(axis=0)
    omega = np.cov(reg, rowvar=False)
    try:
        stat = (n - 1) * gbar @ np.linalg.solve(omega, gbar)
    except np.linalg.LinAlgError:
        return float("nan"), float("nan")
    pvalue = float(stats.chi2.sf(stat, df=h.shape[1]))
    return float(stat), pvalue


def model_confidence_set(
    losses: dict[str, np.ndarray], alpha: float = 0.05, reps: int = 1000, seed: int = 42
) -> list[str]:
    """Model Confidence Set (Hansen-Lunde-Nason 2011): modelos no peores que el mejor.

    ``losses`` mapea modelo -> vector de pérdidas por observación (alineadas). Devuelve
    la lista de modelos que pertenecen al MCS al nivel alpha (el resto se descarta como
    significativamente peores).
    """
    from arch.bootstrap import MCS

    names = list(losses)
    if len(names) < 2:
        return names
    mat = pd.DataFrame({k: losses[k] for k in names}).dropna()
    mcs = MCS(mat, size=alpha, reps=reps, seed=seed)
    mcs.compute()
    included = mcs.included
    return [str(x) for x in included] if len(included) else [str(mat.mean().idxmin())]


def friedman_nemenyi(scores: pd.DataFrame) -> FriedmanResult:
    """Friedman + Nemenyi (Demšar 2006) sobre una matriz series×modelos de una métrica.

    Devuelve el p-valor de Friedman (H0: todos los modelos rinden igual), los rangos
    promedio por modelo (menor = mejor) y la matriz de p-valores pareados de Nemenyi.
    """
    import scikit_posthocs as sp

    clean = scores.dropna(axis=0, how="any")
    stat, p = stats.friedmanchisquare(*[clean[c].to_numpy() for c in clean.columns])
    avg_rank = clean.rank(axis=1).mean().sort_values()
    nemenyi = sp.posthoc_nemenyi_friedman(clean.to_numpy())
    nemenyi.index = nemenyi.columns = clean.columns
    return {"friedman_stat": float(stat), "friedman_p": float(p), "avg_rank": avg_rank, "nemenyi": nemenyi}


def dedup_series(
    df: pd.DataFrame, value: str = "hold_mase", keys: tuple[str, ...] = ("country", "category")
) -> tuple[pd.DataFrame, int, int]:
    """Colapsa pseudo-réplicas del corte mundial a UNA representante (B2).

    Varias series (India/China/All-Charg. en ciertas categorías) comparten el corte
    mundial: sus vectores de ``value`` por modelo son idénticos fila a fila. Contarlas
    todas infla n, estrecha los tests pareados (Wilcoxon/Nemenyi anticonservadores) y
    sobrepondera el corte mundial en las medias. Devuelve
    ``(df_filtrado, n_series_raw, n_series_efectivas)``; el llamador debe REPORTAR el
    n efectivo junto a cada media.
    """
    piv = df.pivot_table(index=list(keys), columns="model", values=value)
    keep = piv[~piv.duplicated()].index
    out = df[df.set_index(list(keys)).index.isin(keep)]
    return out, int(len(piv)), int(len(keep))


def demo() -> None:
    """Self-check: DM detecta un modelo claramente peor; Holm endurece p-valores."""
    rng = np.random.default_rng(1)
    good = rng.normal(0, 1, 200)  # errores chicos
    bad = rng.normal(0, 3, 200)  # errores grandes
    stat, p = dm_test(good, bad)
    assert p < 0.01 and stat < 0, (stat, p)  # good significativamente mejor

    same = good + rng.normal(0, 1e-9, 200)
    _, p_same = dm_test(good, same)
    assert p_same > 0.05, p_same  # sin diferencia real

    adj = holm({"a": 0.01, "b": 0.04, "c": 0.04})
    assert adj["a"][1] is True  # 0.01*3=0.03 < 0.05
    assert all(adj[k][0] >= raw for k, raw in {"a": 0.01, "b": 0.04, "c": 0.04}.items())

    # Giacomini-White: bueno vs malo -> rechaza igual habilidad condicional.
    _, gw_p = giacomini_white(good, bad)
    assert gw_p < 0.05, gw_p

    # Model Confidence Set: el malo queda fuera; el bueno (y su casi-clon) dentro.
    same = good + rng.normal(0, 1e-6, 200)
    mcs = model_confidence_set({"good": good**2, "clone": same**2, "bad": bad**2}, reps=200)
    # El malo queda fuera; el MCS es un subconjunto de los buenos (gemelos indistinguibles).
    assert "bad" not in mcs and set(mcs) <= {"good", "clone"} and len(mcs) >= 1, mcs

    # Friedman-Nemenyi: 30 series, 3 modelos con error creciente -> ranking las ordena.
    rng2 = np.random.default_rng(7)
    sc = pd.DataFrame(
        {
            "m_bueno": rng2.normal(0.2, 0.05, 30),
            "m_medio": rng2.normal(0.5, 0.05, 30),
            "m_malo": rng2.normal(0.9, 0.05, 30),
        }
    )
    fn = friedman_nemenyi(sc)
    assert fn["friedman_p"] < 0.01
    assert fn["avg_rank"].index[0] == "m_bueno"
    print(
        f"OK — DM p={p:.1e}; GW p={gw_p:.1e}; MCS={mcs}; Friedman p={fn['friedman_p']:.1e} "
        f"mejor={fn['avg_rank'].index[0]}"
    )


if __name__ == "__main__":
    demo()
