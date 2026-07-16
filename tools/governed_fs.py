#!/usr/bin/env python
"""Cuarentena gobernada por transacción (P0R.5 · B148/B145 · Incremento 1R3 · B179/B180). CONCENTRA las primitivas
destructivas del sistema de archivos (`os.unlink`/`os.rmdir` + `rename_*` de `atomic_fs`) fuera de
`campaign_bundle.py`, para que un gate AST prohíba mutaciones crudas en la capa de bundle.

El problema (B179): `check(inode) → os.unlink(name)` tiene una ventana en la que el nombre puede re-ligarse a un
objeto CONCURRENTE que termina borrado. (B180): `rmtree(name)` borra el árbol que ESTÉ en `name`, aunque haya sido
re-ligado a un árbol ajeno. La cura: NUNCA borrar por nombre tras un check. En su lugar, MOVER el objeto (rename
atómico) a un directorio de cuarentena PRIVADO de la transacción (0700, nonce), verificar que el objeto movido liga
al inode que la transacción POSEE (fd propio) y sólo entonces borrarlo DENTRO del privado (sin carrera, nadie más lo
conoce). Un objeto ajeno/mutado se DEVUELVE al origen si es posible y jamás se borra. Journal mínimo durable
(`MANIFEST.jsonl`, INTENT/MOVED/DELETED/PRESERVED, secuencia + hash encadenado)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat

from tools.atomic_fs import AtomicRenameError, AtomicUnsupportedError, rename_noreplace

_QUAR_PREFIX = ".merge-quar"
_JOURNAL = "MANIFEST.jsonl"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class GovernedRemovalError(Exception):
    """No se pudo remover de forma gobernada; el objeto ajeno/mutado se PRESERVA (nunca se borra a ciegas)."""


def _canon(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _ident(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    off = 0
    while off < len(mv):
        n = os.write(fd, mv[off:])
        if n <= 0:
            raise GovernedRemovalError("escritura de journal incompleta")
        off += n


class GovernedQuarantine:
    """Cuarentena de una sola transacción. `with GovernedQuarantine(camp_fd) as q: q.remove_owned(...)`."""

    __slots__ = ("camp_fd", "name", "fd", "_jfd", "_seq", "_prev", "_pending")
    camp_fd: int
    name: str
    fd: int
    _jfd: int
    _seq: int
    _prev: str
    _pending: int

    def __init__(self, camp_fd: int) -> None:
        self.camp_fd = camp_fd
        self.name = f"{_QUAR_PREFIX}.{secrets.token_hex(12)}"
        os.mkdir(self.name, 0o700, dir_fd=camp_fd)
        self.fd = os.open(self.name, _DIR_FLAGS, dir_fd=camp_fd)
        try:
            os.fchmod(self.fd, 0o700)
            st = os.fstat(self.fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise GovernedRemovalError("cuarentena ajena/no-dir/modo != 0700")
            self._jfd = os.open(
                _JOURNAL, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
            )
        except BaseException:
            os.close(self.fd)
            raise
        self._seq = 0
        self._prev = ""
        self._pending = 0  # objetos preservados (no borrados) → la cuarentena no puede cerrarse limpia

    def _journal(self, record: str, **fields: object) -> None:
        self._seq += 1
        rec = {"seq": self._seq, "record": record, "previous_record_sha256": self._prev, **fields}
        sha = hashlib.sha256(_canon(rec)).hexdigest()
        rec["record_sha256"] = sha
        _write_all(self._jfd, _canon(rec) + b"\n")
        os.fsync(self._jfd)
        self._prev = sha

    def _bind(self, obj: str, expected: tuple[int, int]) -> bool:
        fd = os.open(obj, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            return _ident(os.fstat(fd)) == expected
        finally:
            os.close(fd)

    def _bind_dir(self, obj: str, expected: tuple[int, int]) -> bool:
        fd = os.open(obj, _DIR_FLAGS, dir_fd=self.fd)
        try:
            return _ident(os.fstat(fd)) == expected
        finally:
            os.close(fd)

    def remove_owned(self, dir_fd: int, name: str, owned_ident: tuple[int, int]) -> None:
        """Elimina el FICHERO `name` bajo `dir_fd` que la transacción POSEE (owned_ident = dev/ino de su fd). Mueve a
        cuarentena, verifica binding y borra en privado. Objeto ajeno → devuelve al origen y eleva
        `GovernedRemovalError` (jamás borra un objeto que no liga al fd de la transacción, B179)."""
        obj = f"f.{secrets.token_hex(8)}"
        self._journal("INTENT", kind="file", dest=obj, expected=list(owned_ident))
        try:
            rename_noreplace(dir_fd, name, self.fd, obj)
        except (AtomicRenameError, AtomicUnsupportedError, FileNotFoundError, OSError, ValueError) as exc:
            raise GovernedRemovalError(f"no se pudo mover {name!r} a cuarentena: {exc}") from exc
        if not self._bind(obj, owned_ident):  # el objeto movido NO es el que la tx posee → ajeno/mutado
            self._return_or_preserve(obj, dir_fd, name, is_dir=False)
            raise GovernedRemovalError(f"{name!r} no ligaba al fd de la transacción; preservado, no borrado")
        self._journal("MOVED", dest=obj)
        os.unlink(obj, dir_fd=self.fd)  # privado + inode verificado → sin carrera
        self._journal("DELETED", dest=obj)

    def remove_tree_owned(self, parent_fd: int, name: str, owned_ident: tuple[int, int]) -> None:
        """Elimina el ÁRBOL `name` bajo `parent_fd` que la transacción POSEE. Mueve a cuarentena, verifica binding y
        borra recursivamente en privado. Árbol ajeno → devuelve al origen y eleva `GovernedRemovalError` (B180)."""
        obj = f"d.{secrets.token_hex(8)}"
        self._journal("INTENT", kind="tree", dest=obj, expected=list(owned_ident))
        try:
            rename_noreplace(parent_fd, name, self.fd, obj)
        except FileNotFoundError:
            self._journal("ABSENT", dest=obj)
            return
        except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
            raise GovernedRemovalError(f"no se pudo mover el árbol {name!r} a cuarentena: {exc}") from exc
        if not self._bind_dir(obj, owned_ident):
            self._return_or_preserve(obj, parent_fd, name, is_dir=True)
            raise GovernedRemovalError(f"árbol {name!r} no ligaba al fd de la transacción; preservado, no borrado")
        self._journal("MOVED", dest=obj)
        self._rmtree_private(obj)
        self._journal("DELETED", dest=obj)

    def _return_or_preserve(self, obj: str, dir_fd: int, name: str, *, is_dir: bool) -> None:
        try:
            rename_noreplace(self.fd, obj, dir_fd, name)  # devolver el ajeno a su lugar
            self._journal("RETURNED", dest=obj)
        except AtomicRenameError, AtomicUnsupportedError, OSError, ValueError:
            self._pending += 1  # no se pudo devolver → queda PRESERVADO en cuarentena (evidencia)
            self._journal("PRESERVED", dest=obj, kind="dir" if is_dir else "file")

    def _rmtree_private(self, name: str) -> None:
        """Remoción recursiva DENTRO de la cuarentena privada (sin carrera). Valida dir/UID/sin symlinks/especiales."""
        fd = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                raise GovernedRemovalError(f"árbol en cuarentena {name!r} ajeno/no-dir")
            for entry in os.listdir(fd):
                est = os.lstat(entry, dir_fd=fd)
                if stat.S_ISLNK(est.st_mode):
                    raise GovernedRemovalError(f"symlink en árbol de cuarentena: {entry!r}")
                if stat.S_ISDIR(est.st_mode):
                    self._rmtree_private_at(fd, entry)
                elif stat.S_ISREG(est.st_mode):
                    os.unlink(entry, dir_fd=fd)
                else:
                    raise GovernedRemovalError(f"objeto especial en árbol de cuarentena: {entry!r}")
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=self.fd)

    def _rmtree_private_at(self, parent_fd: int, name: str) -> None:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            for entry in os.listdir(fd):
                est = os.lstat(entry, dir_fd=fd)
                if stat.S_ISLNK(est.st_mode) or not (stat.S_ISDIR(est.st_mode) or stat.S_ISREG(est.st_mode)):
                    raise GovernedRemovalError(f"objeto no regular en árbol de cuarentena: {entry!r}")
                if stat.S_ISDIR(est.st_mode):
                    self._rmtree_private_at(fd, entry)
                else:
                    os.unlink(entry, dir_fd=fd)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent_fd)

    def close(self, errors: list[str] | None = None) -> None:
        """Cierra el journal y, si NO quedan objetos preservados, elimina el directorio de cuarentena. Si quedan
        preservados (ajenos que no se pudieron devolver), lo DEJA como evidencia (no borra). Reporta, no eleva."""
        errs = errors if errors is not None else []
        jfd = getattr(self, "_jfd", -1)
        if jfd >= 0:
            try:
                os.close(jfd)
            except OSError as exc:
                errs.append(f"cerrar journal de cuarentena: {exc}")
            self._jfd = -1
        fd = getattr(self, "fd", -1)
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as exc:
                errs.append(f"cerrar fd de cuarentena: {exc}")
            self.fd = -1
        if self._pending == 0:
            try:
                self._remove_empty_quarantine()
            except OSError as exc:
                errs.append(f"remover cuarentena vacía: {exc}")

    def _remove_empty_quarantine(self) -> None:
        qfd = os.open(self.name, _DIR_FLAGS, dir_fd=self.camp_fd)
        try:
            leftover = [e for e in os.listdir(qfd) if e != _JOURNAL]
            if leftover:
                return  # algo quedó → no se borra (evidencia)
            os.unlink(_JOURNAL, dir_fd=qfd)
        finally:
            os.close(qfd)
        os.rmdir(self.name, dir_fd=self.camp_fd)

    def __enter__(self) -> GovernedQuarantine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
