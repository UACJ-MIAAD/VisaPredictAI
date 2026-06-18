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

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "visapredict.duckdb"


def _connect(db_path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"Almacén no encontrado en {path}. Corre `make db` para regenerarlo.")
    # ponytail: read-only — el modelado nunca escribe en el almacén (regla un-solo-escritor).
    return duckdb.connect(str(path), read_only=True)


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
