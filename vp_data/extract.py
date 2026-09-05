"""C2 — extracción por país: un solo algoritmo, parametrizado.

Los dos scrapers tenían el MISMO `extract_country_data` escrito dos veces. Comparados línea a
línea, solo diferían en tres cosas: el nombre de la columna de nivel (`EB_level` / `F_level`),
el clasificador que la traduce a código canónico, y los comentarios. El resto —el caso especial
de `row`, la selección de columna por etiqueta normalizada, la omisión de tablas defectuosas, el
esquema del marco vacío, la anotación de fechas, la etiqueta cruda preservada, el descarte de
filas ajenas, la deduplicación y el orden— era idéntico. Duplicación accidental, no dos reglas.

Aquí queda una vez, en funciones puras que se pueden probar con datos sintéticos. Lo que NO vive
aquí: la red, el parseo de HTML, la detección de secciones y la escritura de CSV siguen siendo
responsabilidad de cada scraper.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pandas as pd

from vp_data.visa_common import annotate_dates, norm_label

# Columnas que el marco vacío debe declarar además de la de nivel. Vive aquí porque el panel
# las exige: un país sin filas escribía antes un CSV incompleto y `build_panel` fallaba lejos
# de la causa (J8).
EMPTY_EXTRA_COLS: tuple[str, ...] = (
    "priority_date",
    "visa_bulletin_date",
    "table_type",
    "raw_value",
    "status",
    "visa_wait_time",
    "raw_category",
)

Classifier = Callable[[object], str | None]


def search_term(country: str) -> str:
    """Texto a buscar en la cabecera de columna para ese país.

    `row` (resto del mundo) vive en la columna «all chargeability areas except those listed»;
    se busca «except those listed», que se mantiene estable incluso cuando los boletines viejos
    parten «chargeability» como «charge ability»."""
    return "except those listed" if country == "row" else country


def select_country_columns(df: pd.DataFrame, search: str) -> tuple[object, object] | None:
    """(columna de categoría, columna del país) o `None` si la tabla no sirve.

    La categoría es siempre la columna 0 (su cabecera ha sido «employment-based»,
    «family- sponsored», «family» o vacía según el año). El país se busca por subcadena sobre la
    etiqueta NORMALIZADA, que absorbe `\\xa0`, saltos de línea, espacios dobles y mayúsculas de
    más de veinte años de formatos."""
    norm = {col: norm_label(col) for col in df.columns}
    cat_col = df.columns[0]
    country_col = next((c for c in df.columns if search in norm[c]), None)
    if country_col is None or country_col == cat_col:
        return None
    return cat_col, country_col


def empty_country_frame(level_col: str) -> pd.DataFrame:
    """Marco vacío con el esquema REAL de la salida, para que un país sin filas no escriba un
    CSV que el panel rechace después."""
    return pd.DataFrame(columns=[level_col, *EMPTY_EXTRA_COLS])


def preserve_raw_label(labels: pd.Series) -> pd.Series:
    """La etiqueta publicada, tal cual, antes de normalizarla — es el linaje que documenta
    veinte años de deriva en `dim_category_alias`.

    El `rstrip` por línea no es cosmético: los boletines de archivo de 2009 publican etiquetas
    multilínea con espacios al final de cada línea, y dejarlos escribe espacio final dentro del
    campo entrecomillado del CSV, que el hook de espacios del repo reescribe después (churn)."""
    return labels.astype(str).str.strip().str.replace(r"[ \t]+\n", "\n", regex=True)


def classify_rows(df: pd.DataFrame, level_col: str, classifier: Classifier) -> pd.DataFrame:
    """Traduce la etiqueta cruda a código canónico y descarta lo que no es una categoría."""
    out = df.copy()
    out[level_col] = out[level_col].apply(classifier)
    return out[out[level_col].notna()]


def dedupe(df: pd.DataFrame, level_col: str) -> pd.DataFrame:
    """Clave única (categoría, mes, tabla), quedándose con la PRIMERA.

    Una transición de etiqueta puede poner la misma categoría canónica dos veces en un mismo
    boletín (el split «Unreserved» de EB-5 en mayo de 2022)."""
    return df.drop_duplicates(subset=[level_col, "visa_bulletin_date", "table_type"], keep="first")


def extract_country_data(
    country: str,
    all_data: Sequence[pd.DataFrame],
    *,
    level_col: str,
    classifier: Classifier,
) -> pd.DataFrame:
    """Las filas de un país a partir de las tablas parseadas de todos los boletines."""
    search = search_term(country)

    country_data: list[pd.DataFrame] = []
    for df in all_data:
        cols = select_country_columns(df, search)
        if cols is None:
            continue
        cat_col, country_col = cols
        try:
            sub = df[[cat_col, country_col, "visa_bulletin_date", "table_type"]].copy()
        except KeyError, ValueError:
            # ValueError: una cabecera normalizada duplicada hace que `df[country_col]` sea un
            # marco, así que el rename de abajo no cuadraría. Esa tabla se omite.
            continue
        sub.columns = [level_col, "priority_date", "visa_bulletin_date", "table_type"]
        country_data.append(sub)

    if not country_data:
        return empty_country_frame(level_col)

    country_df = pd.concat(country_data, axis=0, ignore_index=True)
    country_df = country_df[country_df["visa_bulletin_date"].notna()]
    country_df = annotate_dates(country_df, "priority_date")
    country_df["raw_category"] = preserve_raw_label(country_df[level_col])
    country_df = classify_rows(country_df, level_col, classifier)
    return dedupe(country_df, level_col)
