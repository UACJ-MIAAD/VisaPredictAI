#!/usr/bin/env python
"""Lectura snapshot gobernada de un CSV RELATIVA a un descriptor de directorio (P0R.5 · R9.2R5/R9.2R6/B95/B96).
Compartida por `merge_campaign_pools` y `check_deep_refit` para que la evidencia se lea igual en ambos:

0. `name` debe ser un NOMBRE RELATIVO simple (B96): str no vacío, `== os.path.basename(name)`, no absoluto, sin
   separadores (`/`, `os.sep`, `os.altsep`), sin NUL y `∉ {".", ".."}`. Una ruta peligrosa se RECHAZA, no se
   normaliza — un nombre absoluto/`..` haría que `os.open(..., dir_fd=)` IGNORE el descriptor y escape del árbol.
1. `openat(dir_fd, name, O_RDONLY|O_NOFOLLOW|O_NONBLOCK)` — un symlink revienta (no se sigue) y un FIFO/socket
   sustituido NO cuelga el open (B217/B218); el tipo se valida por `fstat` ANTES de leer.
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

import contextlib
import os
import stat

import pandas as pd


class GovernedOpenError(Exception):
    """B217/B218/B219: el objeto en un nombre gobernado NO es un fichero regular apto para lectura — tipo especial
    (FIFO/socket/dispositivo) o UID/nlink/modo inesperados. Es un error de DOMINIO (no `OSError`) para que un
    `except OSError` genérico jamás lo trague en silencio y para distinguirlo de una AUSENCIA (`FileNotFoundError`)."""


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


@contextlib.contextmanager
def opened_regular_noblock_at(dir_fd: int, name: str, *, uid: int | None = None, nlink: int | None = None, mode: int | None = None):  # fmt: skip
    """FUENTE ÚNICA de apertura de lectura segura (B217/B218/B219). Abre `name` (nombre RELATIVO simple) bajo
    `dir_fd` con `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` — un FIFO/socket sustituido NO cuelga el `open()` esperando un
    escritor — hace `fstat` del MISMO descriptor y EXIGE `S_ISREG` (+ UID/nlink/modo si se piden) ANTES de ceder el
    fd; NUNCA se lee el contenido de un objeto especial. Cede `(fd, st)` y CIERRA el fd en TODA salida.
    `FileNotFoundError` si el nombre está ausente; `GovernedOpenError` (de dominio, nunca cruda) si es especial o no
    cumple los atributos exigidos."""
    prob = relative_name_problem(name)
    if prob is not None:
        raise GovernedOpenError(f"nombre no gobernado {name!r}: {prob}")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)  # FIFO/socket no cuelga
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):  # objeto especial → jamás se lee su contenido
            raise GovernedOpenError(f"{name!r} no es un fichero regular (FIFO/socket/dispositivo)")
        if uid is not None and st.st_uid != uid:
            raise GovernedOpenError(f"{name!r} de UID inesperado ({st.st_uid} != {uid})")
        if nlink is not None and st.st_nlink != nlink:
            raise GovernedOpenError(f"{name!r} con nlink inesperado ({st.st_nlink} != {nlink})")
        if mode is not None and stat.S_IMODE(st.st_mode) != mode:
            raise GovernedOpenError(f"{name!r} con modo inesperado ({oct(stat.S_IMODE(st.st_mode))} != {oct(mode)})")
        yield fd, st
    finally:
        os.close(fd)


_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    data = b""
    while chunk := os.read(fd, 1 << 16):
        data += chunk
    return data


@contextlib.contextmanager
def opened_regular_noblock_path(root_fd: int, rel: str, *, uid: int | None = None, nlink: int | None = None, mode: int | None = None):  # fmt: skip
    """Helper de rutas gobernadas POR COMPONENTE (B218). Camina `rel` (relativo, multi-componente) desde `root_fd`
    abriendo CADA directorio con `O_DIRECTORY | O_NOFOLLOW` — ningún ancestro puede ser un symlink — y abre el leaf
    con la primitiva `opened_regular_noblock_at` (no bloqueante + S_ISREG antes de leer). Cede `(fd, st)` y cierra
    TODOS los descriptores en toda salida."""
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or os.path.isabs(rel):
        raise GovernedOpenError(f"ruta gobernada inválida {rel!r}")
    fds: list[int] = []
    try:
        cur = root_fd
        for comp in parts[:-1]:
            nfd = os.open(comp, _DIR_OPEN_FLAGS, dir_fd=cur)  # ancestro: dir real, no symlink
            fds.append(nfd)
            cur = nfd
        with opened_regular_noblock_at(cur, parts[-1], uid=uid, nlink=nlink, mode=mode) as (fd, st):
            yield fd, st
    finally:
        for fd in reversed(fds):
            os.close(fd)


def read_bytes_at(dir_fd: int, name: str) -> bytes:
    """Lee el contenido COMPLETO de un fichero regular `name` (nombre relativo simple) bajo `dir_fd`, no bloqueante."""
    with opened_regular_noblock_at(dir_fd, name) as (fd, _st):
        return _read_all(fd)


def read_bytes_path(root_fd: int, rel: str) -> bytes:
    """Lee el contenido COMPLETO de un fichero gobernado por componente `rel` desde `root_fd` (cada dir O_NOFOLLOW)."""
    with opened_regular_noblock_path(root_fd, rel) as (fd, _st):
        return _read_all(fd)


def read_bytes_abs(path: str | os.PathLike) -> bytes:
    """Lee un fichero por RUTA (posiblemente absoluta) de forma NO BLOQUEANTE: abre el directorio contenedor con
    `O_DIRECTORY | O_NOFOLLOW` (el symlink FINAL no se sigue) y el leaf regular con la primitiva. Para lecturas de
    rutas FIJAS de módulo/entorno/contrato; los ancestros del contenedor NO se gobiernan por componente (la cadena
    completa es R9). `GovernedOpenError` si el leaf es especial; propaga `OSError`/`FileNotFoundError`."""
    d, base = os.path.split(os.fspath(path))
    dfd = os.open(d or ".", _DIR_OPEN_FLAGS)
    try:
        return read_bytes_at(dfd, base)
    finally:
        os.close(dfd)


def _governed_reader(dir_fd: int, name: str, reader):
    """Abre `name` gobernado (nombre relativo + O_NOFOLLOW + fstat regular/UID/nlink==1/no escribible por
    grupo-otros), llama `reader(fd)` DENTRO del snapshot fstat pre/post y devuelve `(resultado, None)` o
    `(None, motivo)`. Cualquier mutación in-place durante `reader` (mismo inode, contenido cambiado) aborta."""
    try:  # B218: la apertura no bloqueante + S_ISREG es la MISMA primitiva (fuente única); aquí sólo las
        with opened_regular_noblock_at(dir_fd, name) as (
            fd,
            st0,
        ):  # exigencias EXTRA de evidencia (UID/nlink/no-escribible/snapshot)
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
    except FileNotFoundError as exc:
        return None, f"ausente ({exc})"
    except GovernedOpenError as exc:
        return None, str(exc)  # nombre no gobernado / objeto especial (no-regular)
    except OSError as exc:
        return None, f"apertura falló ({exc})"


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
    try:  # B218: MISMA primitiva de apertura no bloqueante + S_ISREG (fuente única); el fd VIVO se DUPLICA fuera
        with opened_regular_noblock_at(dir_fd, name) as (fd, st0):  # del context manager (que cierra el suyo al salir)
            if st0.st_uid != os.geteuid():
                return -1, None, "propietario ajeno"
            if st0.st_nlink != 1:
                return -1, None, "hardlink (nlink != 1)"
            if stat.S_IMODE(st0.st_mode) & 0o022:
                return -1, None, "escribible por grupo/otros"
            live = os.dup(fd)  # el lease necesita un fd que sobreviva; la primitiva cierra el suyo
            return live, _snapshot(st0), None
    except FileNotFoundError as exc:
        return -1, None, f"ausente ({exc})"
    except GovernedOpenError as exc:
        return -1, None, str(exc)  # nombre no gobernado / objeto especial (no-regular)
    except OSError as exc:
        return -1, None, f"apertura falló ({exc})"


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
