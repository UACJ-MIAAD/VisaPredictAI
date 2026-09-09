"""E2 · Contrastes EXACTOS para comparar modelos pareados, sin dependencias de modelado.

El protocolo queda **congelado aquí, antes de mirar ningún resultado**. Todo lo que sigue es
decisión tomada por escrito y no se toca según lo que salga:

* **Contraste primario**: rango con signo de Wilcoxon, **bilateral** y **exacto**, con la nula
  obtenida por **permutación de signos condicionada al vector de magnitudes observado**. Bajo la
  hipótesis nula de simetría, cada uno de los ``2^n`` patrones de signo es igual de probable, así
  que la nula condicional es exacta **también cuando hay empates** — que es donde la tabla de
  rangos clásica deja de valer. La enumeración se hace por programación dinámica sobre sumas de
  rangos; con ``n`` pequeña, una prueba la compara contra la enumeración explícita.
* **Ceros**: se descartan (convención de Wilcoxon) y ``n_eff`` cuenta solo las diferencias no
  nulas. Aquí importa de verdad: muchas series están congeladas, así que un modelo puede empatar
  EXACTAMENTE con el naïve-1 en varias de ellas.
* **Empates en magnitud**: se usan **rangos medios**. La nula condicional los admite sin
  corrección; el intervalo de Hodges-Lehmann sí supone continuidad, así que cuando hay empates se
  marca ``ties_present`` y el intervalo se lee como aproximado.
* **Tamaño de efecto**: estimador de **Hodges-Lehmann** (mediana de los promedios de Walsh) con
  intervalo de confianza libre de distribución derivado de la MISMA nula exacta. Se acompaña de la
  mediana simple de las diferencias.
* **Prueba de signos exacta**: se calcula siempre como acompañante (binomial bilateral). No
  sustituye al contraste primario; sirve para ver si la conclusión depende de las magnitudes.

Nada de esto decide qué modelos entran ni qué series se miran: eso lo fija el llamador con
criterios nominales.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np

__all__ = [
    "MIN_PAIRS",
    "ResultadoExacto",
    "holm_adjust",
    "hodges_lehmann",
    "midranks",
    "signed_rank_exact",
    "sign_test_exact",
]

#: Mínimo de pares para que un contraste bilateral exacto PUEDA rechazar al 5 %: con n = 6 la
#: cola bilateral extrema vale 2/2^6 = 0.03125. Con menos, la prueba no puede decir nada y el
#: llamador no debe fingir que sí. Congelado antes de calcular.
MIN_PAIRS = 6


@dataclass(frozen=True)
class ResultadoExacto:
    """Resultado del contraste primario sobre un vector de diferencias pareadas."""

    n_pairs: int  # pares disponibles (antes de descartar ceros)
    n_eff: int  # diferencias distintas de cero
    n_zeros: int
    ties_present: bool
    statistic: float  # W+ (suma de rangos medios de las diferencias positivas)
    p_value: float  # bilateral EXACTO por permutación de signos
    hodges_lehmann: float
    ci_low: float
    ci_high: float
    ci_level: float
    sign_test_p: float  # acompañante, nunca sustituto

    def as_dict(self) -> dict:
        return {
            "n_pairs": self.n_pairs,
            "n_eff": self.n_eff,
            "n_zeros": self.n_zeros,
            "ties_present": self.ties_present,
            "statistic": self.statistic,
            "p_value": self.p_value,
            "hodges_lehmann": self.hodges_lehmann,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "ci_level": self.ci_level,
            "sign_test_p": self.sign_test_p,
        }


def midranks(valores: np.ndarray) -> np.ndarray:
    """Rangos medios (1..n) de ``valores``; los empates comparten su promedio."""
    v = np.asarray(valores, dtype="float64")
    orden = np.argsort(v, kind="stable")
    rangos = np.empty(len(v), dtype="float64")
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[orden[j + 1]] == v[orden[i]]:
            j += 1
        rangos[orden[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return rangos


def _distribucion_sumas(pesos2: np.ndarray) -> np.ndarray:
    """Conteos exactos de todas las sumas de subconjuntos de ``pesos2`` (enteros).

    Es la nula condicional de permutación de signos: cada subconjunto = las posiciones que
    reciben signo positivo, y los ``2^n`` patrones son equiprobables bajo simetría.
    """
    total = int(pesos2.sum())
    conteos = np.zeros(total + 1, dtype=object)
    conteos[0] = 1
    for w in pesos2:
        w = int(w)
        if w:
            conteos[w:] = conteos[w:] + conteos[:-w]
        else:
            conteos = conteos * 2
    return conteos


def _p_bilateral(conteos: np.ndarray, obs2: int) -> float:
    """Cola bilateral exacta alrededor del centro de simetría de la nula."""
    total2 = len(conteos) - 1
    centro = Fraction(total2, 2)
    desvio = abs(Fraction(int(obs2)) - centro)
    masa = sum(int(c) for s, c in enumerate(conteos) if c and abs(Fraction(s) - centro) >= desvio)
    return float(Fraction(masa, int(sum(int(c) for c in conteos))))


def sign_test_exact(d: np.ndarray) -> float:
    """Prueba de signos bilateral exacta (binomial) sobre las diferencias no nulas."""
    nz = np.asarray(d, dtype="float64")
    nz = nz[nz != 0.0]
    n = len(nz)
    if n == 0:
        return 1.0
    k = int((nz > 0).sum())
    from math import comb

    total = 2**n
    extremo = min(k, n - k)
    masa = 2 * sum(comb(n, i) for i in range(extremo + 1))
    if n % 2 == 0 and extremo == n // 2:
        masa -= comb(n, n // 2)
    return min(1.0, float(Fraction(masa, total)))


def hodges_lehmann(d: np.ndarray) -> float:
    """Mediana de los promedios de Walsh ``(d_i + d_j)/2`` con ``i <= j``."""
    v = np.asarray(d, dtype="float64")
    i, j = np.triu_indices(len(v))
    return float(np.median((v[i] + v[j]) / 2.0))


def _corte_intervalo(n: int, ci_level: float, n_walsh: int) -> int:
    """Índice de corte del intervalo libre de distribución de Hodges-Lehmann.

    Usa la nula CLÁSICA sin empates (rangos 1..n), que es la base del intervalo. Cuando el vector
    observado trae empates, el intervalo se lee como aproximado y por eso el resultado marca
    ``ties_present``; el valor p, en cambio, sale de la nula condicional y sigue siendo exacto.
    """
    conteos = _distribucion_sumas(np.arange(1, n + 1, dtype=np.int64))
    total = 2**n
    alfa = (1.0 - ci_level) / 2.0
    acumulada, k = 0, 0
    for c in conteos:
        if (acumulada + int(c)) / total > alfa:
            break
        acumulada += int(c)
        k += 1
    return max(0, min(k, (n_walsh - 1) // 2))


def signed_rank_exact(d: np.ndarray, ci_level: float = 0.95) -> ResultadoExacto:
    """Contraste primario congelado: Wilcoxon bilateral exacto por permutación de signos."""
    v = np.asarray(d, dtype="float64")
    if not np.all(np.isfinite(v)):
        raise ValueError("las diferencias pareadas deben ser finitas")
    n_pairs = len(v)
    nz = v[v != 0.0]
    n_eff, n_zeros = len(nz), n_pairs - len(nz)
    if n_eff == 0:
        raise ValueError("todas las diferencias son cero: el contraste no está definido")

    mag = np.abs(nz)
    rangos = midranks(mag)
    ties = bool(len(np.unique(mag)) != len(mag))
    pesos2 = np.rint(rangos * 2).astype(np.int64)
    w_mas2 = int(pesos2[nz > 0].sum())
    conteos = _distribucion_sumas(pesos2)
    p = _p_bilateral(conteos, w_mas2)

    hl = hodges_lehmann(nz)
    walsh = np.sort(np.add.outer(nz, nz)[np.triu_indices(n_eff)] / 2.0)
    corte = _corte_intervalo(n_eff, ci_level, len(walsh))
    return ResultadoExacto(
        n_pairs=n_pairs,
        n_eff=n_eff,
        n_zeros=n_zeros,
        ties_present=ties,
        statistic=float(w_mas2 / 2.0),
        p_value=p,
        hodges_lehmann=hl,
        ci_low=float(walsh[corte]),
        ci_high=float(walsh[len(walsh) - 1 - corte]),
        ci_level=ci_level,
        sign_test_p=sign_test_exact(nz),
    )


def holm_adjust(p_por_hipotesis: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni sobre UNA familia de hipótesis. El llamador define la familia."""
    if not p_por_hipotesis:
        return {}
    orden = sorted(p_por_hipotesis.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(orden)
    ajustadas: dict[str, float] = {}
    previo = 0.0
    for i, (clave, p) in enumerate(orden):
        valor = min(1.0, (m - i) * p)
        previo = max(previo, valor)  # monotonía escalonada
        ajustadas[clave] = previo
    return ajustadas
