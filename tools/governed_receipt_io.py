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


def write_receipt(name: str, data: dict, *, authorized_dir: str = ".") -> None:
    """Escribe `data` (JSON determinista) en `name` DENTRO de `authorized_dir` (abierto como fd de directorio). No
    sobrescribe (`O_EXCL`), no sigue symlink (`O_NOFOLLOW` en leaf y sin cadena de ancestros), 0600, fstat + fsync."""
    prob = _simple_name_problem(name)
    if prob is not None:
        raise ValueError(prob)
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    dfd = os.open(authorized_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
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
    regular/uid/nlink==1, read-to-limit y re-fstat de identidad. Sin cadena de ancestros que un symlink pueda desviar."""
    prob = _simple_name_problem(name)
    if prob is not None:
        raise ValueError(prob)
    dfd = os.open(authorized_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
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
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > _RECEIPT_MAX_BYTES:
                    raise ValueError(f"el recibo excede el máximo {_RECEIPT_MAX_BYTES}")
                chunks.append(chunk)
            st1 = os.fstat(fd)
            if (st0.st_dev, st0.st_ino, st0.st_size) != (st1.st_dev, st1.st_ino, st1.st_size):
                raise ValueError("el inode del recibo cambió durante la lectura")
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return b"".join(chunks)
