"""Cono de coherencia transversal (AL5/F1): FAD<=DFF y país<=AllCharg.

El Visa Bulletin publica restricciones de orden que los pronósticos por serie
ignoran individualmente:

  * FAD <= DFF para la misma (país, categoría, mes): una fecha de filing nunca
    puede preceder a la de acción final en el espacio days_since_base. El panel
    tiene 6 inversiones históricas REALES, así que el dato crudo ocasionalmente
    la viola — pero un PRONÓSTICO coherente no debe hacerlo.
  * país sobresuscrito <= all_chargeability para la misma (categoría, tabla,
    mes): el límite por país solo puede empujar el corte hacia atrás.

Proyección (isotónica min/max simple, la política auditada en la campaña AQ):

  * tope de país: país' = min(país, all_chargeability) — la fila de referencia
    (all_chargeability) se mantiene fija y se recorta al país violador;
  * par FAD/DFF: FAD' = min(FAD, DFF), DFF' = max(FAD, DFF) — los estadísticos
    de orden del par (la alternativa L2-óptima, promediar, mueve ambos miembros;
    min/max preserva los dos valores publicados).
  * bandas (lo80/hi80/lo95/hi95): se DESPLAZAN con el mismo delta que el punto,
    preservando su ancho (la incertidumbre calibrada). Recortarlas al cono las
    estrecharía y degradaría la cobertura medida; desplazarlas es la opción
    conservadora y es la política que midió la auditoría (113+30 → 0).

Las pasadas corren tope-de-país primero y FAD/DFF después; la segunda puede en
principio reabrir una violación de país (DFF' sube), así que las violaciones
residuales se CUENTAN honestamente tras ambas pasadas (post no se asume 0).

ÚNICA implementación (single-source): el publicador web
(``experiments/generate_web_forecasts.py``) proyecta cada añada al cono ANTES de
serializar y expone los contadores pre/post en ``web_forecasts_meta.json``;
``experiments/apply_cone_constraints.py`` importa estas mismas funciones para la
auditoría retrospectiva de artefactos ya publicados.
"""

from __future__ import annotations

import pandas as pd

REFERENCE_COUNTRY = "all_chargeability"
OVERSUBSCRIBED = ("mexico", "india", "china", "philippines")
BAND_COLS = ("lo80", "hi80", "lo95", "hi95")


def count_fad_dff_violations(df: pd.DataFrame) -> int:
    """Celdas (país, categoría, fecha) donde el pronóstico FAD excede al DFF."""
    wide = df.pivot_table(index=["country", "category", "date"], columns="table", values="days", aggfunc="first")
    if "FAD" not in wide.columns or "DFF" not in wide.columns:
        return 0
    both = wide.dropna(subset=["FAD", "DFF"])
    return int((both["FAD"] > both["DFF"]).sum())


def count_country_violations(df: pd.DataFrame) -> int:
    """Filas donde un país sobresuscrito excede el pronóstico de all_chargeability."""
    ref = df[df["country"] == REFERENCE_COUNTRY].set_index(["category", "table", "date"])["days"]
    sub = df[df["country"].isin(OVERSUBSCRIBED)]
    ref_days = ref.reindex(pd.MultiIndex.from_frame(sub[["category", "table", "date"]])).to_numpy()
    mask = pd.notna(ref_days) & (sub["days"].to_numpy() > ref_days)
    return int(mask.sum())


def _shift_row(df: pd.DataFrame, idx: pd.Index, new_days: pd.Series) -> None:
    """Fija ``days`` en ``new_days`` sobre ``idx`` y desplaza las bandas con el mismo delta."""
    delta = new_days - df.loc[idx, "days"]
    for col in BAND_COLS:
        df.loc[idx, col] = df.loc[idx, col] + delta
    df.loc[idx, "days"] = new_days


def apply_country_cap(df: pd.DataFrame) -> pd.DataFrame:
    """país' = min(país, all_chargeability) por (categoría, tabla, fecha)."""
    df = df.copy()
    ref = df[df["country"] == REFERENCE_COUNTRY].set_index(["category", "table", "date"])["days"]
    sub_idx = df.index[df["country"].isin(OVERSUBSCRIBED)]
    keys = pd.MultiIndex.from_frame(df.loc[sub_idx, ["category", "table", "date"]])
    ref_days = pd.Series(ref.reindex(keys).to_numpy(), index=sub_idx)
    viol = ref_days.notna() & (df.loc[sub_idx, "days"] > ref_days)
    if viol.any():  # sin violaciones no se toca nada (preserva dtypes: passthrough exacto)
        _shift_row(df, sub_idx[viol], ref_days[viol])
    return df


def apply_fad_dff(df: pd.DataFrame) -> pd.DataFrame:
    """(FAD', DFF') = (min, max) del par por (país, categoría, fecha)."""
    df = df.copy()
    key_cols = ["country", "category", "date"]
    fad = df[df["table"] == "FAD"].set_index(key_cols)
    dff = df[df["table"] == "DFF"].set_index(key_cols)
    common = fad.index.intersection(dff.index)
    bad = common[(fad.loc[common, "days"] > dff.loc[common, "days"]).to_numpy()]
    if len(bad):
        fad_pos = df.index[df["table"] == "FAD"][fad.index.get_indexer(bad)]
        dff_pos = df.index[df["table"] == "DFF"][dff.index.get_indexer(bad)]
        lo = pd.Series(dff.loc[bad, "days"].to_numpy(), index=fad_pos)  # min del par
        hi = pd.Series(fad.loc[bad, "days"].to_numpy(), index=dff_pos)  # max del par
        _shift_row(df, fad_pos, lo)
        _shift_row(df, dff_pos, hi)
    return df


def project(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Proyecta ``df`` al cono y devuelve ``(proyectado, contadores)``.

    Contadores: ``cone_violations_pre``/``cone_violations_post`` (totales, los que
    vigilan el correo SES y los gates) + desglose por restricción. Sin violaciones,
    devuelve el frame de entrada INTACTO (passthrough byte-estable: mismos valores,
    mismos dtypes, mismo orden de filas).
    """
    pre_country = count_country_violations(df)
    pre_pair = count_fad_dff_violations(df)
    projected = apply_fad_dff(apply_country_cap(df)) if (pre_country or pre_pair) else df
    post_country = count_country_violations(projected)
    post_pair = count_fad_dff_violations(projected)
    counters = {
        "cone_violations_pre": int(pre_country + pre_pair),
        "cone_violations_post": int(post_country + post_pair),
        "cone_violations_detail": {
            "country_le_allcharg": {"pre": int(pre_country), "post": int(post_country)},
            "fad_le_dff": {"pre": int(pre_pair), "post": int(post_pair)},
        },
    }
    return projected, counters
