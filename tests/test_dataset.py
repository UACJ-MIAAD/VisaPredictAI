"""Contrato de la capa de carga de series para modelado (US-A2).

Requiere el almacén DuckDB (`make db`). Si no existe, los tests se omiten en vez
de fallar, para no romper CI en ramas que no reconstruyen la BD.
"""

from __future__ import annotations

import pytest

from vp_model import dataset

pytestmark = pytest.mark.skipif(not dataset.DB_PATH.exists(), reason="almacén DuckDB ausente (corre `make db`)")


def test_load_series_is_f_only_and_nonnegative() -> None:
    s = dataset.load_series("mexico", "F3", "FAD")
    assert len(s) > 0
    # days_since_base solo existe para status='F' y t0=1975 antecede toda prioridad.
    assert (s >= 0).all()


def test_fad_and_dff_never_mixed() -> None:
    fad = dataset.load_series("mexico", "F3", "FAD")
    dff = dataset.load_series("mexico", "F3", "DFF")
    # FAD arranca en 2001-12; DFF no existe antes de 2015-10.
    assert fad.index.min() < dff.index.min()


def test_invalid_table_rejected() -> None:
    with pytest.raises(ValueError):
        dataset.load_series("mexico", "F3", "final_action")


def test_reindex_only_adds_nan_gaps() -> None:
    sparse = dataset.load_series("china", "F1", "FAD")
    dense = dataset.load_series("china", "F1", "FAD", reindex_monthly=True)
    assert len(dense) >= len(sparse)
    # dtype-insensible: si la serie no tiene huecos (china F1 quedó completa tras
    # la resurrección I1), reindex no introduce NaN y el dtype se queda en int64.
    assert dense.dropna().astype("float64").equals(sparse.astype("float64"))


def test_list_series_pilot_only() -> None:
    cat = dataset.list_series(table="FAD", block="family")
    assert set(cat["country"]).issubset(set(dataset.PILOT_COUNTRIES))
    assert (cat["table"] == "FAD").all()
