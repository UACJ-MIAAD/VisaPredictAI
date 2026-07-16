#!/usr/bin/env python
"""Lectura snapshot gobernada de un CSV RELATIVA a un descriptor de directorio (P0R.5 · R9.2R5/B95).
Compartida por `merge_campaign_pools` y `check_deep_refit` para que la evidencia se lea igual en ambos:

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


def read_governed_csv(dir_fd: int, name: str, **read_csv_kwargs) -> tuple[pd.DataFrame | None, str | None]:
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
        with os.fdopen(fd, "rb", closefd=False) as fh:
            df = pd.read_csv(fh, **read_csv_kwargs)
        if _snapshot(os.fstat(fd)) != snap0:
            return None, "mutado durante la lectura (snapshot fstat pre/post distinto)"
        return df, None
    finally:
        os.close(fd)
