"""E6 · La ÚNICA autoridad del universo evaluable. Derivada, nunca escrita a mano.

Antes de E6 el universo se derivaba en seis sitios: el mart de DuckDB, el censo del EDA sobre el
parquet, el censo de cohortes sobre el CSV, dos artefactos sellados y un envoltorio en
``horizon``. **Medido: los seis coincidían exactamente en las mismas 74 series** — no había
deriva viva. Lo que sí había era la *posibilidad* de que se separaran sin que nadie se enterara,
y conteos cableados que la habrían tapado.

Este módulo es la autoridad: se deriva del panel **canónico** (``vp_data.config.PANEL_PATH``, el
CSV versionado) con la definición única ``dataset.is_evaluable``, y todo consumidor la llama en
vez de guardar su propia lista o su propio número. Los artefactos sellados (``series_cohorts``,
``e4_router_pool``) siguen siendo **salidas** con su procedencia, no fuentes paralelas: una
prueba comprueba que coinciden con esta autoridad.

Dependency-light a propósito (solo pandas): el corredor profundo vive en un entorno aislado.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from vp_data.config import PANEL_PATH

__all__ = ["Clave", "evaluable_keys", "n_evaluable", "structural_keys"]

Clave = tuple[str, str, str]  # (país, categoría, tabla)


def _leer_panel(ruta: Path) -> pd.DataFrame:
    """El panel canónico es el CSV versionado; el .parquet es una salida de DuckDB."""
    panel = pd.read_parquet(ruta) if ruta.suffix == ".parquet" else pd.read_csv(ruta)
    panel["bulletin_date"] = pd.to_datetime(panel["bulletin_date"])
    return panel


@lru_cache(maxsize=4)
def _censo(ruta: Path) -> tuple[tuple[Clave, bool], ...]:
    from vp_model.dataset import is_evaluable

    panel = _leer_panel(ruta)
    filas: list[tuple[Clave, bool]] = []
    for (pais, _bloque, categoria, tabla), g in panel.groupby(["country", "block", "category", "table"], sort=True):
        f = g[g.status == "F"]
        if not len(f):
            filas.append(((pais, categoria, tabla), False))
            continue
        meses = f.bulletin_date.dt.to_period("M")
        span = int((meses.max() - meses.min()).n) + 1
        filas.append(((pais, categoria, tabla), bool(is_evaluable(len(f), span, tabla))))
    return tuple(filas)


def structural_keys(panel_path: Path | None = None) -> list[Clave]:
    """Todas las series estructurales del panel, ordenadas."""
    return [k for k, _ in _censo(panel_path or PANEL_PATH)]


def evaluable_keys(panel_path: Path | None = None) -> list[Clave]:
    """Las series EVALUABLES, ordenadas. Esta lista es la autoridad; no se copia."""
    return [k for k, ok in _censo(panel_path or PANEL_PATH) if ok]


def n_evaluable(panel_path: Path | None = None) -> int:
    """Cuántas son. Derivado: quien escriba el número a mano se queda atrás en el próximo corte."""
    return len(evaluable_keys(panel_path))
