"""E1 · Cohortes de estabilidad calculadas SOLO con información pre-split.

Dirección del director (17-ago-2026): separar series estables de no estables y entrenar
cada grupo por su lado. Antes de poder hacerlo hay que poder *decir* qué es estable sin
mirar el hold-out, porque una partición que use la ventana de evaluación convierte
cualquier comparación posterior en una profecía autocumplida.

**Todo se mide estrictamente antes de** ``holdout_start``:

    holdout_start = to_regular_monthly_causal(raw).index[-HOLDOUT]

El corte se calcula sobre la rejilla mensual CAUSAL (la misma que ve el walk-forward), no
sobre el índice disperso de observaciones F: dos series con el mismo número de F pero
distinta continuidad tendrían orígenes distintos si se contara por posición.

**Trampa que esta partición evita** (medida el 26-ago-2026): «estable = cero
retrogresiones» selecciona 23 series, todas DFF, congeladas entre el 54 % y el 82 % del
tiempo y con paso mediano de 0 días. Son «estables» porque **no se mueven**, que es
justo el régimen donde el naïve-1 es imbatible por construcción. Por eso la regla mide
**volatilidad** (retrogresiones) y la **quietud** viaja aparte, como anotación descriptiva:
quien compare cohortes después tiene que poder ver cuál de las dos cosas está mirando.

La regla está **congelada** (``RULE_VERSION``) y es deliberadamente pobre: dos umbrales,
ninguno ajustado por su resultado. E1 y E2 son **exploratorios**; sólo E3/E4 —o datos
posteriores al registro— pueden tratarse como confirmación.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from vp_model.config import HOLDOUT
from vp_model.preprocess import to_regular_monthly_causal
from vp_model.scale import naive_scale_before

__all__ = [
    "COHORTES",
    "FEATURE_NAMES",
    "RECENT_WINDOW_MONTHS",
    "RETRO_RATE_MAX",
    "RULE_VERSION",
    "WORST_RETRO_SCALED_MAX",
    "PreSplitFeatures",
    "annotate",
    "classify",
    "etiqueta_celda",
    "holdout_start",
    "nombre_visible",
    "pre_split",
    "pre_split_features",
]

#: Versión de la regla de cohorte. Congelada: cambiarla es un evento, no un ajuste.
RULE_VERSION = "1.0.0"

#: Umbrales de RULE v1.0.0, registrados ANTES de mirar ningún resultado de modelado.
RETRO_RATE_MAX = 0.02
WORST_RETRO_SCALED_MAX = 5.0

#: Las DOS cohortes que produce ``classify``. No hay una tercera (ver su docstring).
COHORTES = frozenset({"estable", "no_estable"})

#: Identificador -> nombre visible. Sólo cambia el que lleva guion bajo; ver ``nombre_visible``.
_NOMBRE_VISIBLE = {"no_estable": "inestable"}

#: Ventana de recencia de la anotación ``retro_reciente`` (meses antes del corte).
RECENT_WINDOW_MONTHS = 36

FEATURE_NAMES = (
    "retro_rate_pre",
    "worst_retro_scaled_pre",
    "continuity_pre",
    "pct_frozen_pre",
    "median_step_pre",
    "step_cv_pre",
    "retro36_pre",
)


@dataclass(frozen=True)
class PreSplitFeatures:
    """Las siete features, todas sobre observaciones anteriores al corte.

    Son las mismas fórmulas del censo (``experiments/build_eda_facts.py``) restringidas a
    la ventana pre-split, más dos que el censo no tiene: la escala de la peor retrogresión
    y la dispersión del paso.
    """

    retro_rate_pre: float  # fracción de pasos F con retroceso
    worst_retro_scaled_pre: float  # peor retroceso ÷ escala naïve estacional de la serie
    continuity_pre: float  # meses con F ÷ meses del tramo
    pct_frozen_pre: float  # fracción de pasos con avance exactamente 0
    median_step_pre: float  # avance mediano por paso F (días)
    step_cv_pre: float  # desviación ÷ |media| del paso (NaN si la media es 0)
    retro36_pre: int  # retrocesos en los últimos RECENT_WINDOW_MONTHS antes del corte
    n_pre: int  # observaciones F usadas (no es feature: es el tamaño de la evidencia)

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def holdout_start(raw: pd.Series, holdout: int = HOLDOUT) -> pd.Timestamp:
    """Primer mes del hold-out, sobre la rejilla mensual causal."""
    if holdout < 1:
        raise ValueError(f"holdout debe ser >= 1, no {holdout!r}")
    rejilla = to_regular_monthly_causal(raw)
    if len(rejilla) <= holdout:
        raise ValueError(f"serie demasiado corta: {len(rejilla)} meses de rejilla <= holdout {holdout}")
    return pd.Timestamp(rejilla.index[-holdout])


def pre_split(raw: pd.Series, corte: pd.Timestamp) -> pd.Series:
    """Observaciones F ESTRICTAMENTE anteriores al corte."""
    return raw[raw.index < corte]


def pre_split_features(raw: pd.Series, corte: pd.Timestamp) -> PreSplitFeatures:
    """Las siete features sobre ``raw`` restringida a ``< corte``.

    ``worst_retro_scaled_pre`` usa la escala naïve estacional de la MISMA ventana
    (``vp_model.scale``, fuente única de E0): un retroceso de 400 días no significa lo
    mismo en una serie que avanza 30 días al mes que en una que avanza 3.
    """
    f = pre_split(raw, corte).astype("float64")
    if f.empty:
        raise ValueError(f"sin observaciones F antes de {corte.date()}")
    pasos = f.diff().dropna()
    n_pasos = len(pasos)

    if n_pasos:
        peor = float(-pasos.min()) if float(pasos.min()) < 0 else 0.0
        retro_rate = float((pasos < 0).mean())
        pct_frozen = float((pasos == 0).mean())
        mediana = float(pasos.median())
        media = float(pasos.mean())
        cv = float(pasos.std(ddof=1) / abs(media)) if n_pasos > 1 and media != 0 else float("nan")
        desde = corte - pd.DateOffset(months=RECENT_WINDOW_MONTHS)
        recientes = pasos[pasos.index >= desde]
        retro36 = int((recientes < 0).sum())
    else:
        peor = retro_rate = pct_frozen = mediana = 0.0
        cv = float("nan")
        retro36 = 0

    escala = naive_scale_before(raw, corte)
    peor_escalado = peor / escala if np.isfinite(escala) else float("nan")

    meses = f.index.to_period("M")
    tramo = int((meses.max() - meses.min()).n) + 1
    return PreSplitFeatures(
        retro_rate_pre=retro_rate,
        worst_retro_scaled_pre=peor_escalado,
        continuity_pre=len(f) / tramo,
        pct_frozen_pre=pct_frozen,
        median_step_pre=mediana,
        step_cv_pre=cv,
        retro36_pre=retro36,
        n_pre=len(f),
    )


def classify(f: PreSplitFeatures) -> str:
    """RULE v1.0.0 — congelada.

    ``no_estable`` si ``retro_rate_pre > 0.02`` **o** ``worst_retro_scaled_pre > 5``;
    ``estable`` en cualquier otro caso.

    Una entrada no finita **no se clasifica**: ``NaN > 5`` es ``False`` en Python, así que
    dejarla pasar convertiría «no se pudo medir» en «estable». Falla cerrado.
    """
    for nombre in ("retro_rate_pre", "worst_retro_scaled_pre"):
        valor = getattr(f, nombre)
        if not np.isfinite(valor):
            raise ValueError(f"{nombre} no finito ({valor!r}): la serie no es clasificable por RULE v{RULE_VERSION}")
    inestable = f.retro_rate_pre > RETRO_RATE_MAX or f.worst_retro_scaled_pre > WORST_RETRO_SCALED_MAX
    return "no_estable" if inestable else "estable"


def nombre_visible(cohort: str) -> str:
    """Cómo se PRESENTA una cohorte en la tabla, en la figura y en el texto.

    ``no_estable`` se muestra como «inestable» porque el guion bajo obliga a escaparlo en
    LaTeX (``no\\_estable``) y entonces la celda del `.tex` deja de ser literalmente igual a
    la clave del JSON, que es justo lo que compara el contrato `table` del guardián.

    El mapa vive aquí, junto a ``classify``, y no en cada consumidor: en M65 la tabla decía
    «inestable» y la figura «no_estable» para la MISMA celda, dos páginas seguidas, porque
    cada superficie se lo construía por su cuenta.

    Falla cerrado ante una cohorte desconocida: si algún día hay una tercera, la surface
    que no sepa nombrarla debe romperse, no imprimir el identificador crudo.
    """
    if cohort not in COHORTES:
        raise ValueError(f"cohorte desconocida {cohort!r}: RULE v{RULE_VERSION} solo produce {sorted(COHORTES)}")
    return _NOMBRE_VISIBLE.get(cohort, cohort)


def etiqueta_celda(table: str, cohort: str) -> str:
    """Nombre de una celda tabla×cohorte, único para el JSON, la tabla y la figura."""
    return f"{table}/{nombre_visible(cohort)}"


def annotate(f: PreSplitFeatures) -> dict[str, bool]:
    """Las DOS anotaciones derivadas. No entran en la regla ni la modifican.

    * ``congelada``: el paso mediano es exactamente 0 — la serie está quieta, que es
      distinto de ser regular. Separa la «quietud» de la «estabilidad» (trampa del 26-ago).
    * ``retro_reciente``: hubo al menos un retroceso en los últimos
      ``RECENT_WINDOW_MONTHS`` meses del tramo pre-split.
    """
    return {"congelada": f.median_step_pre == 0.0, "retro_reciente": f.retro36_pre > 0}
