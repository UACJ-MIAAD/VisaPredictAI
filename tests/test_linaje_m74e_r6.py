"""M74-E-R6 · REDs PERMANENTES del gate de linaje (auditoría `8bc41ff6…`, B3).

R5 declaró tres ataques contra `tools/check_source_lineage.py` pero sólo «directorio vacío» quedó
como prueba; «archivo ausente» y «bytes alterados» fueron ensayos manuales. Y la auditoría
reprodujo un cuarto que pasaba: nombre y hash correctos con `vintage='1900-01-01'`.
Todo corre en el job base: `duckdb`, `pandas` y `bs4` son dependencias de `.[dev]`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tools import check_source_lineage as csl

ELEGIBLES = {"visa-bulletin-for-april-2002.html": "2002-04-01", "visa-bulletin-for-may-2002.html": "2002-05-01"}


def _escena(tmp_path: Path, *, estado: str = "ok", filas=None) -> Path:
    import duckdb

    snaps = tmp_path / "data" / "snapshots"
    snaps.mkdir(parents=True)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    for nombre in ELEGIBLES:
        (snaps / nombre).write_text(f"<html>{nombre}</html>", encoding="utf-8")
    (snaps / "update-on-july-visa-availability.html").write_text("anuncio", encoding="utf-8")  # no elegible
    if filas is None:
        filas = [(n, hashlib.sha256((snaps / n).read_bytes()).hexdigest(), v) for n, v in ELEGIBLES.items()]
    con = duckdb.connect(str(tmp_path / "data" / "processed" / "visapredict.duckdb"))
    con.execute("CREATE TABLE source_artifact (filename VARCHAR, sha256 VARCHAR, vintage DATE)")
    con.execute("CREATE TABLE etl_run (run_id INTEGER, build_status VARCHAR)")
    con.executemany("INSERT INTO source_artifact VALUES (?, ?, ?)", filas)
    con.execute("INSERT INTO etl_run VALUES (1, ?)", [estado])
    con.close()
    return tmp_path


def test_control_un_linaje_coherente_pasa(tmp_path: Path) -> None:
    """Sin control, «falla» podría ser el veredicto de todo y el gate sería un muro."""
    assert csl.verify(_escena(tmp_path)) == {"n_snapshots": 2, "n_linaje": 2}


def test_RED_vintage_falso_con_nombre_y_hash_correctos(tmp_path: Path) -> None:
    """★ El ataque exacto de la auditoría: `vintage='1900-01-01'` con nombre y hash en regla."""

    def sha(nombre: str) -> str:
        return hashlib.sha256(f"<html>{nombre}</html>".encode()).hexdigest()

    abril, mayo = "visa-bulletin-for-april-2002.html", "visa-bulletin-for-may-2002.html"
    raiz = _escena(tmp_path, filas=[(abril, sha(abril), "1900-01-01"), (mayo, sha(mayo), "2002-05-01")])
    with pytest.raises(csl.LineageError, match="vintage"):
        csl.verify(raiz)


def test_RED_directorio_vacio(tmp_path: Path) -> None:
    raiz = _escena(tmp_path)
    for f in (raiz / "data" / "snapshots").iterdir():
        f.unlink()
    with pytest.raises(csl.LineageError, match="no tiene un solo snapshot"):
        csl.verify(raiz)


def test_RED_archivo_ausente(tmp_path: Path) -> None:
    raiz = _escena(tmp_path)
    (raiz / "data" / "snapshots" / "visa-bulletin-for-april-2002.html").unlink()
    with pytest.raises(csl.LineageError, match="sin snapshot en disco"):
        csl.verify(raiz)


def test_RED_bytes_alterados(tmp_path: Path) -> None:
    raiz = _escena(tmp_path)
    (raiz / "data" / "snapshots" / "visa-bulletin-for-may-2002.html").write_text("<html>otro</html>", encoding="utf-8")
    with pytest.raises(csl.LineageError, match="hash"):
        csl.verify(raiz)


def test_RED_snapshot_elegible_sin_fila(tmp_path: Path) -> None:
    raiz = _escena(tmp_path)
    (raiz / "data" / "snapshots" / "visa-bulletin-for-june-2002.html").write_text(
        "<html>nuevo</html>", encoding="utf-8"
    )
    with pytest.raises(csl.LineageError, match="SIN fila de linaje"):
        csl.verify(raiz)


def test_RED_build_degradado(tmp_path: Path) -> None:
    with pytest.raises(csl.LineageError, match="degraded"):
        csl.verify(_escena(tmp_path, estado="degraded"))
