#!/usr/bin/env python
"""Lectura snapshot gobernada de un CSV RELATIVA a un descriptor de directorio (P0R.5 · R9.2R5/R9.2R6/B95/B96).
Compartida por `merge_campaign_pools` y `check_deep_refit` para que la evidencia se lea igual en ambos:

0. `name` debe ser un NOMBRE RELATIVO simple (B96): str no vacío, `== os.path.basename(name)`, no absoluto, sin
   separadores (`/`, `os.sep`, `os.altsep`), sin NUL y `∉ {".", ".."}`. Una ruta peligrosa se RECHAZA, no se
   normaliza — un nombre absoluto/`..` haría que `os.open(..., dir_fd=)` IGNORE el descriptor y escape del árbol.
1. `openat(dir_fd, name, O_RDONLY|O_NOFOLLOW)` — un symlink revienta (no se sigue).
2. `fstat` inicial y exigencia de: fichero REGULAR, del UID actual, `nlink == 1` y **sin escritura de grupo/
   otros** (`mode & 0o022 == 0`) — un fichero que un tercero puede reescribir NO es evidencia de confianza.
3. Se registra el snapshot (`st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns, st_uid, st_mode, st_nlink`) y
   pandas lee del MISMO descriptor.
4. Un SEGUNDO `fstat` antes de cerrar; el snapshot pre/post debe ser IDÉNTICO — una mutación in-place durante
   la lectura (mismo inode, contenido cambiado) aborta aunque el DataFrame parseado parezca válido.

Devuelve `(df, None)` en éxito o `(None, motivo)` en fallo — el llamador decide si es `_fail` (merge) o
`return None`/`return 1` (deep). No sigue symlinks ni re-resuelve por ruta.
"""

from __future__ import annotations

import os
import stat

import pandas as pd

# Campos del snapshot que deben ser idénticos pre/post lectura (tamaño/tiempos/identidad).
_SNAP = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_uid", "st_mode", "st_nlink")


def _snapshot(st: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(st, f) for f in _SNAP)


def relative_name_problem(name: str) -> str | None:
    """B96: None si `name` es un nombre RELATIVO simple seguro para `openat(dir_fd=…)`; si no, el motivo. Una
    ruta absoluta o con `..`/separadores haría que `os.open(name, dir_fd=fd)` ignore el descriptor y escape."""
    if not isinstance(name, str) or not name:
        return "nombre vacío o no-string"
    if name in (".", ".."):
        return f"nombre reservado {name!r}"
    if "\x00" in name:
        return "nombre con NUL"
    if os.path.isabs(name):
        return "nombre absoluto (ignoraría el descriptor)"
    seps = {"/", os.sep} | ({os.altsep} if os.altsep else set())
    if any(s in name for s in seps):
        return "nombre con separador de ruta"
    if name != os.path.basename(name):
        return "nombre con componentes de directorio"
    return None


def _governed_reader(dir_fd: int, name: str, reader):
    """Abre `name` gobernado (nombre relativo + O_NOFOLLOW + fstat regular/UID/nlink==1/no escribible por
    grupo-otros), llama `reader(fd)` DENTRO del snapshot fstat pre/post y devuelve `(resultado, None)` o
    `(None, motivo)`. Cualquier mutación in-place durante `reader` (mismo inode, contenido cambiado) aborta."""
    problem = relative_name_problem(name)
    if problem is not None:
        return None, problem
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        return None, f"ausente o symlink ({exc})"
    try:
        st0 = os.fstat(fd)
        if not stat.S_ISREG(st0.st_mode):
            return None, "no-regular"
        if st0.st_uid != os.geteuid():
            return None, "propietario ajeno"
        if st0.st_nlink != 1:
            return None, "hardlink (nlink != 1)"
        if stat.S_IMODE(st0.st_mode) & 0o022:
            return None, "escribible por grupo/otros"
        snap0 = _snapshot(st0)
        result = reader(fd)
        if _snapshot(os.fstat(fd)) != snap0:
            return None, "mutado durante la lectura (snapshot fstat pre/post distinto)"
        return result, None
    finally:
        os.close(fd)


def read_governed_csv(dir_fd: int, name: str, **read_csv_kwargs) -> tuple[pd.DataFrame | None, str | None]:
    def _read(fd: int) -> pd.DataFrame:
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return pd.read_csv(fh, **read_csv_kwargs)

    return _governed_reader(dir_fd, name, _read)


def read_governed_bytes(dir_fd: int, name: str) -> tuple[bytes | None, str | None]:
    """B108: análogo a `read_governed_csv` pero devuelve los BYTES crudos del output previo con snapshot fstat
    pre/post — la copia 'de confianza' (`previous_bytes`) que alimenta la recuperación del rollback debe estar
    igual de gobernada que una lectura de evidencia."""

    def _read(fd: int) -> bytes:
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read()

    return _governed_reader(dir_fd, name, _read)
