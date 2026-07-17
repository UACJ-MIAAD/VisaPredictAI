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
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)  # B217: FIFO no cuelga
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


def snapshot_fd(fd: int) -> tuple[int, ...]:
    """B114/B115: snapshot fstat (los mismos campos que `_governed_reader` exige estables) de un descriptor
    que la transacción mantiene ABIERTO. Un lease se revalida comparando este snapshot pre/post."""
    return _snapshot(os.fstat(fd))


def digest_fd(fd: int) -> str:
    """sha256 del contenido leído DEL descriptor (no del nombre). Reutilizable por los leases de entrada/salida."""
    os.lseek(fd, 0, os.SEEK_SET)
    import hashlib

    h = hashlib.sha256()
    while chunk := os.read(fd, 1 << 16):
        h.update(chunk)
    return h.hexdigest()


def open_governed_lease(dir_fd: int, name: str) -> tuple[int, tuple[int, ...] | None, str | None]:
    """B114/B115: abre `name` gobernado (nombre relativo + O_NOFOLLOW + regular/UID/nlink==1/no escribible por
    grupo-otros) y devuelve el fd VIVO + su snapshot para que el llamador lo mantenga abierto como LEASE hasta
    el commit. Devuelve `(fd, snapshot, None)` en éxito o `(-1, None, motivo)` en fallo — el fd queda abierto y
    es responsabilidad del llamador cerrarlo. No lee el contenido (eso lo hace el llamador vía `digest_fd`)."""
    problem = relative_name_problem(name)
    if problem is not None:
        return -1, None, problem
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)  # B217: FIFO no cuelga
    except OSError as exc:
        return -1, None, f"ausente o symlink ({exc})"
    st0 = os.fstat(fd)
    if not stat.S_ISREG(st0.st_mode):
        os.close(fd)
        return -1, None, "no-regular"
    if st0.st_uid != os.geteuid():
        os.close(fd)
        return -1, None, "propietario ajeno"
    if st0.st_nlink != 1:
        os.close(fd)
        return -1, None, "hardlink (nlink != 1)"
    if stat.S_IMODE(st0.st_mode) & 0o022:
        os.close(fd)
        return -1, None, "escribible por grupo/otros"
    return fd, _snapshot(st0), None


def lease_problem(dir_fd: int, name: str, fd: int, snapshot: tuple[int, ...], digest: str) -> str | None:
    """B114/B115: revalida un lease VIVO — el `fd` sigue regular/UID/nlink==1/no escribible, su snapshot fstat
    es IDÉNTICO al de la apertura, el `name` dentro de `dir_fd` sigue ligado al MISMO inode (dev/ino) que `fd` y
    el contenido (digest) no cambió. Cualquier divergencia (nombre re-ligado, mutación in-place, truncado,
    chmod, hardlink) devuelve el motivo. None si el lease sigue íntegro."""
    try:
        st = os.fstat(fd)
    except OSError as exc:
        return f"fstat del lease {name!r} falló ({exc})"
    if not stat.S_ISREG(st.st_mode):
        return "lease no-regular"
    if st.st_uid != os.geteuid():
        return "lease de propietario ajeno"
    if st.st_nlink != 1:
        return "lease con hardlink (nlink != 1)"
    if stat.S_IMODE(st.st_mode) & 0o022:
        return "lease escribible por grupo/otros"
    if _snapshot(st) != snapshot:
        return f"lease {name!r} con snapshot fstat distinto (mutación/truncado/chmod)"
    try:
        stn = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        return f"nombre {name!r} del lease ausente/inaccesible ({exc})"
    if (stn.st_dev, stn.st_ino) != (st.st_dev, st.st_ino):
        return f"nombre {name!r} ya no liga al lease (dev/ino distinto)"
    try:
        if digest_fd(fd) != digest:
            return f"contenido del lease {name!r} cambió (digest distinto)"
    except OSError as exc:
        return f"re-digest del lease {name!r} falló ({exc})"
    return None
