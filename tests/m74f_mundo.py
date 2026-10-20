"""M74-F · piezas de los mundos sintéticos de la complementaria: DuckDB temporal y evidencia de la madre.

Compartido por `test_m74f_complemento.py` (contrato) y `test_m74f_complemento_cableado.py` (git real).

* La DuckDB se construye con el builder REAL (`pipeline.build_database.build`) sobre el mini panel de
  `tests.dbfixtures`, en un directorio temporal: nunca se toca ni se reconstruye la base viva. Dos
  builds iguales dan la misma huella lógica con bytes distintos, que es justo lo que el sello tolera.
* `redefinir_vista` cambia SÓLO el SQL de una vista que leen los consumidores (las filas quedan
  iguales): el caso que la huella de filas no ve (auditoría Codex 9-oct).
* La evidencia imita la de la cuarta campaña real: `SHA256SUMS` + copia de `campaign.json` + un tar.gz
  cuyos miembros llevan la ruta del repo y, como el real, hermanos AppleDouble (``._<nombre>``).
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Mapping
from pathlib import Path

import duckdb
import pandas as pd

import pipeline.build_database as bd
from tests.dbfixtures import mini_dv, mini_panel, mini_raw_dir, mini_snapshots

ARCHIVO = "salidas_campana_completa.tar.gz"


def panel_csv() -> str:
    """El panel de los mundos: el mismo mini panel con el que se construye la DuckDB."""
    return mini_panel().to_csv(index=False)


def construir_duckdb(
    destino: Path,
    panel: Path,
    trabajo: Path,
    *,
    git_sha: str = "a" * 40,
    started_at: str = "2026-10-09T00:00:00Z",
    degradada: bool = False,
    meses: int | None = None,
) -> Path:
    """Construye ``destino`` con el builder real; `etl_run.panel_sha256` = sha256 de ``panel``.

    ``trabajo`` (fuera del worktree) recibe los CSV crudos y snapshots que el builder exige.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    trabajo.mkdir(parents=True, exist_ok=True)
    run_info = {
        "pipeline_run_id": "m74f-test",
        "git_sha": git_sha,
        "git_dirty": False,
        "panel_sha256": hashlib.sha256(panel.read_bytes()).hexdigest(),
        "started_at": pd.Timestamp(started_at).to_pydatetime(),
    }
    datos = mini_panel() if meses is None else mini_panel(n_months=meses)
    con = duckdb.connect(str(destino))
    try:
        bd.build(
            con,
            datos,
            None if degradada else mini_dv(),
            raw_dir=mini_raw_dir(trabajo),
            snapshots_dir=mini_snapshots(trabajo),
            run_info=run_info,
            allow_degraded=degradada,
        )
    finally:
        con.close()
    return destino


#: Un cambio de semántica por vista consumida, con las MISMAS filas: (fragmento del SQL, reemplazo).
#: `mart_training_F` da los valores de las series; `mart_series_summary`, cuántas observaciones
#: entrenables tiene cada una (lo que decide cuáles son evaluables).
CAMBIOS_DE_VISTA = {
    "mart_training_F": ("f.days_since_base AS days_since_base", "(f.days_since_base + 123) AS days_since_base"),
    "mart_series_summary": ("count_star() FILTER (WHERE (f.status = 'F')) AS n_trainable",
                            "(count_star() FILTER (WHERE (f.status = 'F')) + 100) AS n_trainable"),
}  # fmt: skip


def redefinir_vista(db: Path, vista: str) -> None:
    """Reescribe la definición de ``vista`` según `CAMBIOS_DE_VISTA`; falla si el fragmento no está."""
    viejo, nuevo = CAMBIOS_DE_VISTA[vista]
    con = duckdb.connect(str(db))
    try:
        fila = con.execute("SELECT sql FROM duckdb_views() WHERE view_name = ?", [vista]).fetchone()
        assert fila is not None and fila[0].count(viejo) == 1, f"{vista}: su SQL no contiene {viejo!r}"
        con.execute(fila[0].replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1).replace(viejo, nuevo))
    finally:
        con.close()


def _miembro(tar: tarfile.TarFile, nombre: str, datos: bytes) -> None:
    info = tarfile.TarInfo(nombre)
    info.size = len(datos)
    tar.addfile(info, io.BytesIO(datos))


def escribir_tar(archivo: Path, miembros: Mapping[str, bytes], *, enlaces: Mapping[str, str] | None = None) -> Path:
    """tar.gz con cada miembro y su hermano AppleDouble, como el de la madre. ``enlaces``: nombre → destino."""
    archivo.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archivo, "w:gz") as tar:
        for nombre, datos in miembros.items():
            carpeta, _, base = nombre.rpartition("/")
            _miembro(tar, f"{carpeta}/._{base}" if carpeta else f"._{base}", b"\x00\x05\x16\x07AppleDouble")
            _miembro(tar, nombre, datos)
        for nombre, destino in (enlaces or {}).items():
            info = tarfile.TarInfo(nombre)
            info.type = tarfile.SYMTYPE
            info.linkname = destino
            tar.addfile(info)
    return archivo


def escribir_manifiesto(carpeta: Path) -> Path:
    """`SHA256SUMS` de todo lo que hay en ``carpeta`` (como el de la evidencia real)."""
    filas = [
        f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}"
        for f in sorted(carpeta.iterdir())
        if f.is_file() and f.name != "SHA256SUMS"
    ]
    manifiesto = carpeta / "SHA256SUMS"
    manifiesto.write_text("\n".join(filas) + "\n")
    return manifiesto


def evidencia_madre(
    carpeta: Path,
    madre: Path,
    artefactos: list[str],
    *,
    extra: Mapping[str, bytes] | None = None,
    enlaces: Mapping[str, str] | None = None,
) -> Path:
    """La evidencia de la madre en ``carpeta``: su `campaign.json`, el tar con ``artefactos`` y el manifiesto."""
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "campaign.json").write_bytes((madre / "reports/campaign/campaign.json").read_bytes())
    (carpeta / "bitacora_resumen.txt").write_text("etapas 1-10 completas\n")
    miembros = {a: (madre / a).read_bytes() for a in artefactos} | dict(extra or {})
    escribir_tar(carpeta / ARCHIVO, miembros, enlaces=enlaces)
    return escribir_manifiesto(carpeta)
