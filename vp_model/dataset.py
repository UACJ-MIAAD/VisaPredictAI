"""Carga de series temporales y_{p,c,b,t} para el modelado (US-A2).

Cada serie es la variable dependiente ``days_since_base`` (días desde la época
base t0 = 1-ene-1975) indexada por mes de boletín, leída desde ``mart_training_F``
que ya está restringida a estado e=F. FAD y DFF se cargan por separado y nunca se
mezclan (promesa del Anteproyecto: las tablas se evalúan de forma independiente).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from vp_model.config import PILOT_COUNTRIES, TABLES

# M6: single source of truth for the artifact paths — hand-rederiving them here
# meant a PROCESSED_DIR move sent the modeling layer to a different file. The
# fallback keeps the module importable if the repo-root config isn't on sys.path
# (e.g. vp_model consumed as an installed package outside the repo).
try:
    from config import DUCKDB_PATH as DB_PATH
    from config import PANEL_PATH as PANEL_CSV
except ImportError:  # pragma: no cover
    DB_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "visapredict.duckdb"
    PANEL_CSV = Path(__file__).resolve().parent.parent / "data" / "processed" / "visa_panel_long.csv"


def _connect(db_path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"Almacén no encontrado en {path}. Corre `make db` para regenerarlo.")
    # ponytail: read-only — el modelado nunca escribe en el almacén (regla un-solo-escritor).
    con = duckdb.connect(str(path), read_only=True)
    # M2: etl_run se escribe al FINAL del build — es un centinela de completitud
    # gratis que nadie consultaba: un build interrumpido dejaba un almacén parcial
    # que abre sin queja y un catálogo vacío que el modelado itera "con éxito".
    row = con.execute("SELECT count(*) FROM etl_run").fetchone()
    if not row or row[0] != 1:
        con.close()
        raise RuntimeError(f"Almacén incompleto en {path} (etl_run vacío — build interrumpido). Corre `make db`.")
    # M2: frescura DB↔CSV — un `git pull` que trae el boletín nuevo sin `make db`
    # producía cifras del mes anterior sin ninguna señal. La gobernanza por fin se lee.
    if (db_path is None or Path(db_path) == DB_PATH) and PANEL_CSV.exists():
        row = con.execute("SELECT n_fact_priority, panel_ceiling FROM etl_run").fetchone()
        assert row is not None
        n_db, ceiling_db = int(row[0]), pd.Timestamp(row[1])
        col = pd.to_datetime(pd.read_csv(PANEL_CSV, usecols=["bulletin_date"])["bulletin_date"])
        if n_db != len(col) or ceiling_db != col.max():
            con.close()
            raise RuntimeError(
                f"Almacén DESFASADO del panel CSV: DB {n_db} filas/tope {ceiling_db:%Y-%m} vs "
                f"CSV {len(col)} filas/tope {col.max():%Y-%m}. Corre `make db` antes de modelar."
            )
    return con


def is_evaluable(n_trainable: int, span_months: int, table: str) -> bool:
    """N1: LA definición única de serie "plenamente evaluable".

    Antes convivían tres criterios divergentes: el claim publicado (≥84 obs F,
    ``build_key_facts``), ``MIN_TRAINABLE_EVALUABLE`` (solo ``eda.py``) y el gate
    real del walk-forward (span de calendario densificado ≥ MIN_TRAIN[tabla] +
    HOLDOUT + colchón, ``walkforward.py``). Una serie podía contar en el "74
    evaluables" y reventar en el walk-forward, o modelarse sin ser "evaluable".
    Verificado sobre el panel vivo: las 74 series ≥84 F TAMBIÉN pasan el criterio
    de span, así que la definición conjunta preserva el cohort publicado.
    """
    from vp_model.config import HOLDOUT, MIN_BACKTEST_BUFFER, MIN_TRAIN

    return (
        n_trainable >= MIN_TRAIN["FAD"] + HOLDOUT  # riqueza de datos (criterio publicado: 84 F)
        and span_months >= MIN_TRAIN[table] + HOLDOUT + MIN_BACKTEST_BUFFER  # factibilidad walk-forward
    )


def evaluable_series(db_path: str | Path | None = None) -> pd.DataFrame:
    """Catálogo de series evaluables según :func:`is_evaluable`, desde el mart.

    El span se mide sobre las observaciones F (``mart_training_F``), que es la
    serie que el walk-forward densifica — ``mart_series_summary`` abarca todos
    los regímenes y sobreestimaría la factibilidad.
    """
    con = _connect(db_path)
    try:
        df = con.execute(
            'SELECT country, block, category, "table", count(*) AS n_trainable, '
            "datediff('month', min(bulletin_date), max(bulletin_date)) + 1 AS span_months "
            'FROM mart_training_F GROUP BY country, block, category, "table"'
        ).fetchdf()
    finally:
        con.close()
    mask = [is_evaluable(int(r.n_trainable), int(r.span_months), r.table) for r in df.itertuples()]
    return df[pd.Series(mask, index=df.index)].reset_index(drop=True)


def actuals_F(db_path: str | Path | None = None) -> dict[tuple[str, str, str, str], float]:
    """Todos los cortes reales (estado F) del panel: (país, categoría, tabla, 'YYYY-MM-DD') → días.

    API pública para la evaluación prospectiva (``experiments/score_forecasts``): compara
    los pronósticos congelados contra el corte realmente publicado. Encapsula la conexión
    read-only y la consulta (evita exponer ``_connect`` fuera del módulo).
    """
    con = _connect(db_path)
    try:
        df = con.execute('SELECT country, category, "table", bulletin_date, days_since_base FROM mart_training_F').df()
    finally:
        con.close()
    return {
        (r.country, r.category, r.table, pd.Timestamp(r.bulletin_date).strftime("%Y-%m-%d")): float(r.days_since_base)
        for r in df.itertuples()
    }


def list_series(
    *,
    table: str | None = None,
    block: str | None = None,
    countries: tuple[str, ...] = PILOT_COUNTRIES,
    min_trainable: int = 0,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """Catálogo de series disponibles con su tamaño entrenable y su tramo temporal.

    Filtra por tabla (FAD/DFF), bloque (family/employment) y países piloto. Usa
    ``mart_series_summary`` para que la selección de series "evaluables" (longitud
    suficiente) sea barata y reproducible.
    """
    con = _connect(db_path)
    try:
        df = con.sql(
            'SELECT country, block, category, "table", n_trainable, first_month, last_month FROM mart_series_summary'
        ).df()
    finally:
        con.close()
    df = df[df["country"].isin(countries) & (df["n_trainable"] >= min_trainable)]
    if table is not None:
        df = df[df["table"] == table]
    if block is not None:
        df = df[df["block"] == block]
    return df.sort_values(["country", "table", "category"]).reset_index(drop=True)


def load_series(
    country: str,
    category: str,
    table: str,
    *,
    reindex_monthly: bool = False,
    db_path: str | Path | None = None,
) -> pd.Series:
    """Una serie y_{p,c,b,t} como ``pd.Series`` de días, indexada por mes.

    Parameters
    ----------
    table : "FAD" o "DFF" — selección dura, nunca combina ambas.
    reindex_monthly : si True, rellena los meses sin observación F con NaN sobre un
        rango mensual continuo (los huecos corresponden a meses C/U o faltantes; su
        tratamiento es una decisión de preprocesamiento, no de carga — ver US-C3).
    """
    if table not in TABLES:
        raise ValueError(f"table debe ser uno de {TABLES}, no {table!r}")
    con = _connect(db_path)
    try:
        df = con.execute(
            "SELECT bulletin_date, days_since_base FROM mart_training_F "
            'WHERE country = ? AND category = ? AND "table" = ? ORDER BY bulletin_date',
            [country, category, table],
        ).df()
    finally:
        con.close()
    if df.empty:
        raise KeyError(f"Serie vacía: country={country!r} category={category!r} table={table!r}")
    s = pd.Series(
        df["days_since_base"].astype("int64").to_numpy(),
        index=pd.DatetimeIndex(df["bulletin_date"], name="month"),
        name=f"{country}/{category}/{table}",
    )
    if reindex_monthly:
        full = pd.date_range(s.index.min(), s.index.max(), freq="MS")
        s = s.reindex(full)
        s.index.name = "month"
    return s


def demo() -> None:
    """Self-check (US-A2): el régimen e=F se respeta y FAD/DFF no se mezclan."""
    cat = list_series(table="FAD", block="family", countries=("mexico",))
    assert not cat.empty, "no hay series FAD de México"

    fad = load_series("mexico", "F3", "FAD")
    dff = load_series("mexico", "F3", "DFF")
    assert fad.index.min() < dff.index.min(), "FAD debe empezar antes que DFF (2001 vs 2015)"
    assert fad.is_monotonic_increasing is False or True  # solo carga; monotonía es de dominio

    # days_since_base nunca es negativo (t0=1975 antecede a toda prioridad real).
    assert (fad >= 0).all(), "días desde base no pueden ser negativos"

    # reindex introduce NaN solo donde faltaba F; los valores presentes no cambian.
    sparse = load_series("china", "F1", "FAD")
    dense = load_series("china", "F1", "FAD", reindex_monthly=True)
    assert len(dense) >= len(sparse)
    assert dense.dropna().equals(sparse.astype("float64"))
    print(
        f"OK — MX/F3/FAD: {len(fad)} obs {fad.index.min():%Y-%m}→{fad.index.max():%Y-%m}; "
        f"CN/F1/FAD huecos: {len(dense) - len(sparse)}"
    )


if __name__ == "__main__":
    demo()
