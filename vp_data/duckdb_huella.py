"""Huella LÓGICA del almacén DuckDB, legible desde `vp_model` (M74-F · sello de la complementaria).

Vive en `vp_data` por la dirección de capas (ADR-0001): `vp_model.complement` sella la DuckDB que
consumen los lanes GBM y conformal, y `vp_model` no puede importar `pipeline`. Es la MISMA huella que
`pipeline.db_governance.content_fingerprint` (el contrato de determinismo del almacén), y una prueba
exige que den el mismo valor sobre un build real. No se consolidan aún en una sola: `db_governance.py`
es dependencia de la etapa DVC de la base, y moverla obligaría a refrescar `dvc.lock`.

El `.duckdb` no es byte-reproducible (reloj de la bitácora, orden interno de DuckDB): dos builds con
las mismas entradas dan bytes distintos y esta huella igual.

La huella de filas NO ve las vistas: `mart_training_F` y `mart_series_summary`, que es lo que leen los
consumidores (`vp_model.dataset`), son SQL sobre las tablas base, y cambiar su definición cambia lo que
recibe el modelo con las mismas filas (auditoría Codex 9-oct: +123 días en las 120 observaciones con la
huella idéntica). `huella_catalogo` sella ese SQL —y el resto del catálogo de usuario— como componente
SEPARADO, para no alterar el contrato heredado de `content_fingerprint`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb

#: Columnas de reloj de pared: fuera de la huella a propósito (las mismas que en `db_governance`).
COLUMNAS_VOLATILES = {
    "etl_run": {"built_at_utc", "started_at", "completed_at"},
    "schema_version": {"applied_at"},
}


def huella_contenido(db_path: str | Path) -> str:
    """sha256 sobre todas las tablas base, todas las filas, en orden total; sin columnas de reloj."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute("SET TimeZone='UTC'")
        digest = hashlib.sha256()
        tablas = [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()]
        for tabla in tablas:
            fuera = COLUMNAS_VOLATILES.get(tabla, set())
            cols = [r[1] for r in con.execute(f"PRAGMA table_info('{tabla}')").fetchall() if r[1] not in fuera]
            lista = ", ".join(f'"{c}"' for c in cols)
            digest.update(f"## {tabla}({lista})\n".encode())
            for fila in con.execute(f'SELECT {lista} FROM "{tabla}" ORDER BY {lista}').fetchall():
                digest.update(repr(fila).encode())
                digest.update(b"\n")
        return digest.hexdigest()
    finally:
        con.close()


#: El catálogo de usuario de la base: QUÉ es cada objeto, sin oids, tamaños, comentarios ni reloj.
_CATALOGO = (
    ("schema", "SELECT schema_name, sql FROM duckdb_schemas() WHERE {db} AND NOT internal"),
    ("table", "SELECT schema_name, table_name, sql FROM duckdb_tables() WHERE {db}"),
    ("view", "SELECT schema_name, view_name, sql FROM duckdb_views() WHERE {db} AND NOT internal"),
    ("index", "SELECT schema_name, index_name, table_name, sql FROM duckdb_indexes() WHERE {db}"),
    ("sequence", "SELECT schema_name, sequence_name, start_value, min_value, max_value, increment_by, cycle "
                 "FROM duckdb_sequences() WHERE {db}"),
    ("macro", "SELECT schema_name, function_name, function_type, parameters, macro_definition "
              "FROM duckdb_functions() WHERE {db} AND NOT internal"),
    ("type", "SELECT schema_name, type_name, logical_type, labels FROM duckdb_types() WHERE {db} AND NOT internal"),
)  # fmt: skip


def huella_catalogo(db_path: str | Path) -> str:
    """sha256 sobre las definiciones SQL de la base: tablas, VISTAS, índices, secuencias, macros y tipos.

    Las vistas se sellan con sus dependencias: lo que referencian es otra vista (sellada aquí) o una
    tabla base (su definición aquí, sus filas en `huella_contenido`).
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        digest = hashlib.sha256()
        for clase, consulta in _CATALOGO:
            sql = consulta.format(db="database_name = current_database()") + " ORDER BY ALL"
            for fila in con.execute(sql).fetchall():
                digest.update(f"{clase} {fila!r}\n".encode())
        return digest.hexdigest()
    finally:
        con.close()
