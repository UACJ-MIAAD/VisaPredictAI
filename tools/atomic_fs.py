#!/usr/bin/env python
"""Primitivas de rename ATÓMICO fd-relativas (P0R.5 · R9.2R10 · B121/B122/B123/B124/B125).

`os.replace`/`os.rename` NO son CAS: sobrescriben en silencio una colisión del destino (destruyen lo que había)
y, en el patrón validar→`os.replace`, una actualización concurrente entra en la ventana y muere. Aquí se exponen
las dos operaciones del kernel que SÍ son atómicas y sin ventana:

- `rename_noreplace(src_dir_fd, src, dst_dir_fd, dst)`: renombra SOLO si `dst` no existe; si existe, falla con
  `FileExistsError` sin tocar nada (Linux `renameat2(RENAME_NOREPLACE)`; macOS `renameatx_np(RENAME_EXCL)`).
- `rename_exchange(src_dir_fd, src, dst_dir_fd, dst)`: intercambia ATÓMICAMENTE los inodes de `src` y `dst`
  (ambos deben existir; si falta uno, `FileNotFoundError`) — Linux `renameat2(RENAME_EXCHANGE)`; macOS
  `renameatx_np(RENAME_SWAP)`.

FAIL-CLOSED: en una plataforma sin estas syscalls (ni Linux ni Darwin, o el símbolo ausente) CADA llamada
ELEVA `AtomicUnsupportedError`. **Prohibido cualquier fallback a `os.replace`/`os.rename`** — degradar a una
operación no-CAS reintroduce exactamente el bug que estas primitivas cierran.

Errores TIPADOS con `errno`, operación y nombres: `FileExistsError` (EEXIST, colisión en noreplace),
`FileNotFoundError` (ENOENT, falta un extremo), `NotADirectoryError`/`IsADirectoryError` según el errno, y
`AtomicRenameError` (subclase de `OSError`) para el resto — nunca un retorno silencioso.
"""

from __future__ import annotations

import ctypes
import errno as _errno
import os
import platform
import sys

_RENAME_NOREPLACE = 0
_RENAME_EXCHANGE = 1


class AtomicUnsupportedError(RuntimeError):
    """La plataforma no ofrece rename atómico (renameat2/renameatx_np). FAIL-CLOSED: jamás se degrada a
    `os.replace`/`os.rename` (no-CAS). Un despliegue en tal plataforma debe resolverse explícitamente."""


class AtomicRenameError(OSError):
    """Fallo de una primitiva atómica con contexto: `op` ('noreplace'|'exchange'), `src`, `dst` y el errno."""

    def __init__(self, err: int, op: str, src: str, dst: str) -> None:
        super().__init__(err, os.strerror(err))
        self.op = op
        self.src = src
        self.dst = dst

    def __str__(self) -> str:
        return f"{self.op}({self.src!r}->{self.dst!r}): [{self.errno}] {os.strerror(self.errno or 0)}"


def _machine_syscall_nr() -> int | None:
    # __NR_renameat2 por arquitectura (Linux). Fallback solo si el símbolo `renameat2` no existe en libc.
    m = platform.machine()
    return {
        "x86_64": 316,
        "aarch64": 276,
        "arm64": 276,
        "armv7l": 382,
        "armv8l": 382,
        "ppc64le": 357,
        "s390x": 347,
    }.get(m)


class _Backend:
    """Resuelve la syscall nativa UNA vez. `call(op, src_dir_fd, src, dst_dir_fd, dst)` devuelve 0 o lanza el
    error tipado. Fail-closed si la plataforma no es Linux/Darwin o el símbolo no existe."""

    def __init__(self) -> None:
        self.system = platform.system()
        self._fn = None
        self._flags: dict[int, int] = {}
        self._syscall = None
        self._syscall_nr = None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError:
            return
        if self.system == "Linux":
            self._flags = {_RENAME_NOREPLACE: 1, _RENAME_EXCHANGE: 2}  # RENAME_NOREPLACE=1, RENAME_EXCHANGE=2
            if hasattr(libc, "renameat2"):
                fn = libc.renameat2
                fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                fn.restype = ctypes.c_int
                self._fn = fn
            elif hasattr(libc, "syscall"):
                nr = _machine_syscall_nr()
                if nr is not None:
                    sc = libc.syscall
                    sc.restype = ctypes.c_long
                    self._syscall = sc
                    self._syscall_nr = nr
        elif self.system == "Darwin":
            self._flags = {_RENAME_NOREPLACE: 0x4, _RENAME_EXCHANGE: 0x2}  # RENAME_EXCL=0x4, RENAME_SWAP=0x2
            if hasattr(libc, "renameatx_np"):
                fn = libc.renameatx_np
                fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                fn.restype = ctypes.c_int
                self._fn = fn

    def available(self) -> bool:
        return self._fn is not None or (self._syscall is not None and self._syscall_nr is not None)

    def call(self, op: int, src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        if not self.available():
            raise AtomicUnsupportedError(
                f"rename atómico no disponible en {self.system!r}/{platform.machine()!r} — FAIL-CLOSED (sin fallback)"
            )
        flag = self._flags[op]
        sb = os.fsencode(src)
        db = os.fsencode(dst)
        ctypes.set_errno(0)
        if self._fn is not None:
            rc = self._fn(src_dir_fd, sb, dst_dir_fd, db, flag)
        else:
            assert self._syscall is not None and self._syscall_nr is not None
            rc = self._syscall(self._syscall_nr, src_dir_fd, sb, dst_dir_fd, db, flag)
        if rc != 0:
            e = ctypes.get_errno()
            name = "noreplace" if op == _RENAME_NOREPLACE else "exchange"
            if e == _errno.EEXIST:
                raise FileExistsError(e, os.strerror(e), src, None, dst)
            if e == _errno.ENOENT:
                raise FileNotFoundError(e, os.strerror(e), src, None, dst)
            raise AtomicRenameError(e, name, src, dst)


_BACKEND = _Backend()


def supported() -> bool:
    """True si la plataforma ofrece rename atómico real (renameat2/renameatx_np)."""
    return _BACKEND.available()


def rename_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """Renombra `src`→`dst` SOLO si `dst` no existe. `FileExistsError` si existe (sin tocar nada);
    `FileNotFoundError` si falta `src`; `AtomicUnsupportedError` si la plataforma no lo soporta."""
    _BACKEND.call(_RENAME_NOREPLACE, src_dir_fd, src, dst_dir_fd, dst)


def rename_exchange(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """Intercambia ATÓMICAMENTE los inodes de `src` y `dst` (ambos deben existir). `FileNotFoundError` si falta
    alguno; `AtomicUnsupportedError` si la plataforma no lo soporta. Sin ventana: el swap es una sola syscall."""
    _BACKEND.call(_RENAME_EXCHANGE, src_dir_fd, src, dst_dir_fd, dst)


def _selftest() -> int:
    """Ejercita NOREPLACE y EXCHANGE reales de la plataforma (stdlib-only, sin pandas/pytest). Exit 0/1.
    Lo usa el paso de CI en Linux Y macOS para probar que la primitiva nativa funciona en ambos."""
    import tempfile

    if not supported():
        print(f"atomic_fs selftest: NO SOPORTADO en {platform.system()}/{platform.machine()}", file=sys.stderr)
        return 1
    d = tempfile.mkdtemp(prefix="atomicfs.")
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    ok = True

    def _w(name: str, data: bytes) -> None:
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dfd)
        os.write(fd, data)
        os.close(fd)

    def _r(name: str) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        try:
            return os.read(fd, 4096)
        finally:
            os.close(fd)

    try:
        _w("a", b"AAA")
        _w("b", b"BBB")
        # NOREPLACE al ausente: éxito
        rename_noreplace(dfd, "a", dfd, "c")
        assert _r("c") == b"AAA", "noreplace no movió el contenido"
        # NOREPLACE sobre existente: FileExistsError, sin tocar
        try:
            rename_noreplace(dfd, "c", dfd, "b")
            ok = False
            print("FALLO: noreplace sobrescribió un destino existente", file=sys.stderr)
        except FileExistsError:
            assert _r("b") == b"BBB" and _r("c") == b"AAA", "noreplace tocó los inodes en la colisión"
        # EXCHANGE: swap atómico
        rename_exchange(dfd, "c", dfd, "b")
        assert _r("b") == b"AAA" and _r("c") == b"BBB", "exchange no intercambió los inodes"
        # EXCHANGE con extremo ausente: FileNotFoundError
        try:
            rename_exchange(dfd, "nope", dfd, "b")
            ok = False
            print("FALLO: exchange no falló con un extremo ausente", file=sys.stderr)
        except FileNotFoundError:
            pass
    except AssertionError as exc:
        ok = False
        print(f"FALLO: {exc}", file=sys.stderr)
    finally:
        os.close(dfd)
    print(f"atomic_fs selftest: {'OK' if ok else 'FALLO'} en {platform.system()}/{platform.machine()}")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("uso: python -m tools.atomic_fs --selftest", file=sys.stderr)
    sys.exit(2)
