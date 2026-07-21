#!/usr/bin/env python
"""B334: IO GOBERNADO del recibo deep — el productor (deep_smoke) y el validador (validate_deep_receipt) leían/escribían el
recibo con `O_NOFOLLOW` SÓLO en el leaf, de modo que un ANCESTRO symlink dejaba escribir/leer el recibo FUERA del árbol
gobernado. Aquí el recibo es SIEMPRE un NOMBRE SIMPLE (sin separadores/`.`/`..`/NUL) en un directorio AUTORIZADO que se abre
como descriptor de directorio; el leaf se abre RELATIVO a ese fd (`dir_fd=`) con `O_NOFOLLOW`, así que no hay cadena de
ancestros que un symlink pueda desviar. Escritura: `O_CREAT|O_EXCL|O_NOFOLLOW` 0600, fstat regular/uid/nlink==1/modo 0600,
write-all, `fsync` de fichero Y directorio. Lectura: `O_NOFOLLOW|O_NONBLOCK`, fstat regular/uid/nlink==1, read-to-limit,
re-fstat de identidad. Stdlib-only, fail-closed."""

from __future__ import annotations

import json
import os
import stat

_RECEIPT_MAX_BYTES = 1 << 20  # 1 MiB — un recibo deep es pequeño
_GO_WRITE = 0o022  # escritura grupo/otros — prohibida en el recibo


def _simple_name_problem(name: object) -> str | None:
    """El recibo debe ser un NOMBRE SIMPLE: str no vacío, sin NUL, sin separador de ruta, y distinto de `.`/`..`."""
    if type(name) is not str or not name or "\x00" in name:
        return f"nombre de recibo inválido (str no vacío sin NUL): {name!r}"
    if "/" in name or os.sep in name or (os.altsep and os.altsep in name):
        return f"nombre de recibo con separador de ruta (se exige nombre simple): {name!r}"
    if name in (os.curdir, os.pardir):
        return f"nombre de recibo `.`/`..` no permitido: {name!r}"
    return None


def _identity(st: os.stat_result) -> tuple:
    """B337: identidad COMPLETA del recibo para revalidar tras la lectura (no sólo dev/ino/size)."""
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _open_authorized_dir(authorized_dir: str) -> int:
    """B337: abre el directorio autorizado como fd (`O_DIRECTORY|O_NOFOLLOW`) y EXIGE directorio real, del uid actual y sin
    escritura grupo/otros. Devuelve el fd (el caller lo cierra). Fail-closed."""
    dfd = os.open(authorized_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(dfd)
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"directorio autorizado {authorized_dir!r} no es un directorio")
        if st.st_uid != os.getuid():
            raise ValueError(f"directorio autorizado {authorized_dir!r} con dueño uid {st.st_uid} != {os.getuid()}")
        if st.st_mode & _GO_WRITE:
            raise ValueError(f"directorio autorizado {authorized_dir!r} escribible por grupo/otros")
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def write_receipt(name: str, data: dict, *, authorized_dir: str = ".") -> None:
    """Escribe `data` (JSON determinista) en `name` DENTRO de `authorized_dir` (abierto como fd de directorio). No
    sobrescribe (`O_EXCL`), no sigue symlink (`O_NOFOLLOW` en leaf y sin cadena de ancestros), 0600, fstat + fsync."""
    prob = _simple_name_problem(name)
    if prob is not None:
        raise ValueError(prob)
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    dfd = _open_authorized_dir(authorized_dir)  # B337: directorio real, uid actual, sin escritura g/o
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            st0 = os.fstat(fd)
            if not stat.S_ISREG(st0.st_mode):
                raise ValueError("el recibo no es un fichero regular")
            if st0.st_uid != os.getuid():
                raise ValueError(f"el recibo tiene dueño uid {st0.st_uid} != {os.getuid()}")
            if st0.st_nlink != 1:
                raise ValueError(f"el recibo tiene nlink {st0.st_nlink} != 1 (hardlink)")
            if stat.S_IMODE(st0.st_mode) != 0o600:
                raise ValueError(f"el recibo tiene modo {oct(stat.S_IMODE(st0.st_mode))} != 0o600")
            mv = memoryview(payload)
            while mv:  # write-all
                mv = mv[os.write(fd, mv) :]
            os.fsync(fd)
            st1 = os.fstat(fd)  # el descriptor no fue reemplazado bajo nuestros pies
            if (st0.st_dev, st0.st_ino) != (st1.st_dev, st1.st_ino):
                raise ValueError("el inode del recibo cambió durante la escritura")
        finally:
            os.close(fd)
        os.fsync(dfd)  # durabilidad de la entrada de directorio
    finally:
        os.close(dfd)


def read_receipt_bytes(name: str, *, authorized_dir: str = ".") -> bytes:
    """Lee los bytes del recibo `name` DENTRO de `authorized_dir` (fd de directorio); leaf `O_NOFOLLOW|O_NONBLOCK`, fstat
    regular/uid/nlink==1 y **modo EXACTO 0600 (B337)**, read-to-limit y re-fstat de IDENTIDAD COMPLETA (dev/ino/mode/uid/
    nlink/size/mtime_ns/ctime_ns — caza un `chmod` o reemplazo DURANTE la lectura). Sin cadena de ancestros que un symlink
    pueda desviar; los errores de cierre se AGREGAN (un cierre fallido sobre una lectura exitosa es fail-closed)."""
    prob = _simple_name_problem(name)
    if prob is not None:
        raise ValueError(prob)
    dfd = _open_authorized_dir(authorized_dir)  # B337: directorio real, uid actual, sin escritura g/o
    chunks: list[bytes] = []
    close_errors: list[str] = []
    primary: BaseException | None = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        try:
            st0 = os.fstat(fd)
            if not stat.S_ISREG(st0.st_mode):
                raise ValueError("el recibo no es un fichero regular")
            if st0.st_uid != os.getuid():
                raise ValueError(f"el recibo tiene dueño uid {st0.st_uid} != {os.getuid()}")
            if st0.st_nlink != 1:
                raise ValueError(f"el recibo tiene nlink {st0.st_nlink} != 1 (hardlink)")
            if stat.S_IMODE(st0.st_mode) != 0o600:  # B337: modo EXACTO 0600 (0644/0666 rechazados)
                raise ValueError(f"el recibo tiene modo {oct(stat.S_IMODE(st0.st_mode))} != 0o600 exacto")
            total = 0
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > _RECEIPT_MAX_BYTES:
                    raise ValueError(f"el recibo excede el máximo {_RECEIPT_MAX_BYTES}")
                chunks.append(chunk)
            if _identity(os.fstat(fd)) != _identity(st0):  # B337: identidad COMPLETA idéntica tras la lectura
                raise ValueError("la identidad del recibo cambió durante la lectura")
        finally:
            try:
                os.close(fd)
            except OSError as exc:
                close_errors.append(f"leaf: {exc}")
    except BaseException as exc:  # preserva el primario; los cierres se agregan abajo (nunca convierte fallo en éxito)
        primary = exc
    finally:
        try:
            os.close(dfd)
        except OSError as exc:
            close_errors.append(f"dir: {exc}")
    if primary is not None:
        raise primary
    if close_errors:  # lectura exitosa pero un cierre falló → fail-closed
        raise ValueError(f"fallo al cerrar fd(s) del recibo: {'; '.join(close_errors)}")
    return b"".join(chunks)
