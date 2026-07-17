#!/usr/bin/env python
"""Cuarentena gobernada MOVE-ONLY por transacción (P0R.5 · B148/B145 · Incremento 1R4 · B189-B201). Durante la
transacción online NO se borra NADA: los objetos a retirar de la ruta oficial se MUEVEN (rename atómico) a un
directorio de cuarentena DURABLE `.merge-quar.<txid>/` y se PRESERVAN con un journal durable encadenado. El borrado
físico pertenece a un GC POSTERIOR, separado, bajo lock exclusivo y autorización (aún NO implementado).

Causa raíz (B191/B192): `verificar nombre/inode → os.unlink(nombre)` NO es seguro contra un proceso del MISMO UID;
aunque el directorio sea 0700, otro proceso del mismo UID puede sustituir/mutar el objeto en la ventana entre el
check y el unlink. La única cura online es NO borrar: mover-y-preservar. Así no existe ventana check→unlink.

Propiedad por LEASE (B192): mover exige un `OwnedLease` (fd vivo + snapshot COMPLETO + digest + tipo + modo + uid +
nlink) capturado por la transacción; se verifica el lease ANTES y DESPUÉS del move, de modo que una mutación sobre el
MISMO inode (mismo dev/ino, contenido distinto) se detecta y se PRESERVA como ajena, jamás se pierde. Ningún
`os.unlink`/`os.rmdir` aquí: un gate AST fail-closed lo garantiza (`tools/check_raw_fs_mutations.py`)."""

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
# Snapshot SIN timestamps: `rename` bump-ea ctime del inodo, así que comparar ctime/mtime daría falsos positivos en
# todo move legítimo. La mutación de CONTENIDO se detecta por `digest` (ficheros) y el rebind por dev/ino; nlink caza
# hardlinks y el modo, chmod. (dev, ino, nlink, mode) + digest cubre mutación/ajeno/hardlink/chmod sin ruido de reloj.
_SNAP_FIELDS = ("st_dev", "st_ino", "st_nlink", "st_mode")
_INTENT = "INTENT"
_TERMINALS = frozenset({"MOVED", "FOREIGN_PRESERVED", "ABSENT"})
_JOURNAL_KEYS = frozenset({"seq", "record", "operation_id", "dest", "previous_record_sha256", "record_sha256"})


class GovernedRemovalError(Exception):
    """El objeto no coincide con el lease de la transacción; se PRESERVA en cuarentena, jamás se borra ni se pierde."""


class GovernedQuarantineError(Exception):
    """Fallo de construcción/journal/cierre de la cuarentena (durabilidad no garantizable)."""


def _canon(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _snap(st: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(st, f) for f in _SNAP_FIELDS)


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    off = 0
    while off < len(mv):
        n = os.write(fd, mv[off:])
        if n <= 0:
            raise GovernedQuarantineError("escritura de journal incompleta")
        off += n


class OwnedLease:
    """Prueba de PROPIEDAD de un objeto que la transacción abrió: fd vivo + snapshot COMPLETO + digest + tipo. Se
    verifica ANTES y DESPUÉS de moverlo a cuarentena (detecta mutación sobre el mismo inode: B192)."""

    __slots__ = ("fd", "snap", "digest", "is_dir")

    def __init__(self, fd: int, *, is_dir: bool, known_digest: str | None = None) -> None:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise GovernedRemovalError("lease de objeto ajeno (UID)")
        if is_dir and not stat.S_ISDIR(st.st_mode):
            raise GovernedRemovalError("lease de dir sobre no-dir")
        if not is_dir and not stat.S_ISREG(st.st_mode):
            raise GovernedRemovalError("lease de fichero sobre no-regular")
        self.fd = fd
        self.snap = _snap(st)
        self.is_dir = is_dir
        # `known_digest`: para fds O_WRONLY (p. ej. el puntero temporal) cuyo contenido la tx YA conoce (no relegible)
        self.digest = None if is_dir else (known_digest if known_digest is not None else _digest_fd(fd))

    def ident(self) -> tuple[int, int]:
        return (self.snap[0], self.snap[1])


def _digest_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while chunk := os.read(fd, 1 << 16):
        h.update(chunk)
    return h.hexdigest()


class GovernedQuarantine:
    """Cuarentena MOVE-ONLY de una sola transacción. `with GovernedQuarantine(camp_fd, txid) as q: q.quarantine(...)`.
    NUNCA borra: mueve y preserva. El directorio y su journal PERSISTEN (evidencia durable; GC futuro)."""

    __slots__ = ("camp_fd", "txid", "name", "fd", "_jfd", "_seq", "_prev", "_closed")
    camp_fd: int
    txid: str
    name: str
    fd: int
    _jfd: int
    _seq: int
    _prev: str
    _closed: bool

    def __init__(self, camp_fd: int, txid: str) -> None:
        # PEREZOSA: no crea el directorio hasta el primer `quarantine()` — un commit que no necesita retirar nada no
        # deja una cuarentena vacía residual.
        self.camp_fd = camp_fd
        self.txid = txid
        self.name = f"{_QUAR_PREFIX}.{secrets.token_hex(12)}"
        self.fd = -1
        self._jfd = -1
        self._seq = 0
        self._prev = ""
        self._closed = False

    def _ensure(self) -> None:
        if self.fd >= 0:
            return
        os.mkdir(self.name, 0o700, dir_fd=self.camp_fd)
        try:
            self.fd = os.open(self.name, _DIR_FLAGS, dir_fd=self.camp_fd)
        except BaseException:
            raise GovernedQuarantineError(f"no se pudo abrir la cuarentena {self.name}") from None
        try:
            os.fchmod(self.fd, 0o700)
            st = os.fstat(self.fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise GovernedQuarantineError("cuarentena ajena/no-dir/modo != 0700")
            self._jfd = os.open(
                _JOURNAL, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
            )
            os.fsync(self.fd)  # el directorio de cuarentena + su journal son durables antes de operar
        except BaseException:
            os.close(self.fd)  # B194: no dejar un dir con fd abierto; el dir se preserva manifestado (no se borra)
            self.fd = -1
            raise

    def _journal(self, record: str, operation_id: str, dest: str) -> None:
        self._seq += 1
        rec = {"seq": self._seq, "record": record, "operation_id": operation_id, "dest": dest, "previous_record_sha256": self._prev}  # fmt: skip
        sha = hashlib.sha256(_canon(rec)).hexdigest()
        rec["record_sha256"] = sha
        _write_all(self._jfd, _canon(rec) + b"\n")
        os.fsync(self._jfd)  # B200: durabilidad del registro…
        self._reread_and_validate()  # …y RELECTURA + validación de la cadena completa desde el mismo fd
        self._prev = sha

    def _reread_and_validate(self) -> None:
        os.lseek(self._jfd, 0, os.SEEK_SET)
        raw = b""
        while chunk := os.read(self._jfd, 1 << 16):
            raw += chunk
        prev = ""
        seq = 0
        for line in raw.splitlines():
            rec = json.loads(line, object_pairs_hook=_no_dup)
            seq += 1
            if set(rec.keys()) != _JOURNAL_KEYS or rec["seq"] != seq or rec["previous_record_sha256"] != prev:
                raise GovernedQuarantineError("journal de cuarentena con cadena/esquema inválido")
            body = {k: v for k, v in rec.items() if k != "record_sha256"}
            if hashlib.sha256(_canon(body)).hexdigest() != rec["record_sha256"]:
                raise GovernedQuarantineError("journal de cuarentena con hash inválido")
            prev = rec["record_sha256"]

    def quarantine(self, dir_fd: int, name: str, lease: OwnedLease) -> str:
        """MUEVE `name` (bajo `dir_fd`) a la cuarentena y lo PRESERVA. Verifica el LEASE COMPLETO (snapshot+digest)
        del objeto movido: si coincide → MOVED; si difiere (mutación/ajeno) → FOREIGN_PRESERVED + eleva. NUNCA borra;
        el objeto queda durable en la cuarentena. `name` queda libre en la ruta oficial (cero residuo)."""
        if self._closed:
            raise GovernedQuarantineError("cuarentena cerrada")
        self._ensure()  # crea el dir + journal en el primer uso real
        oid = secrets.token_hex(8)
        dest = f"o.{oid}"
        self._journal(_INTENT, oid, dest)
        try:
            rename_noreplace(dir_fd, name, self.fd, dest)
        except FileNotFoundError:
            self._journal("ABSENT", oid, dest)
            return dest
        except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
            raise GovernedRemovalError(f"no se pudo mover {name!r} a cuarentena: {exc}") from exc
        os.fsync(dir_fd)  # origen durable (name ya no está)
        os.fsync(self.fd)  # destino durable
        if self._lease_matches(dest, lease):
            self._journal("MOVED", oid, dest)
            return dest
        self._journal("FOREIGN_PRESERVED", oid, dest)
        raise GovernedRemovalError(f"{name!r} no coincide con el lease de la transacción; PRESERVADO en cuarentena")

    def _lease_matches(self, dest: str, lease: OwnedLease) -> bool:
        flags = _DIR_FLAGS if lease.is_dir else (os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fd = os.open(dest, flags, dir_fd=self.fd)
        except OSError:
            return False
        try:
            if _snap(os.fstat(fd)) != lease.snap:  # snapshot COMPLETO: detecta mutación sobre el mismo inode
                return False
            return lease.is_dir or _digest_fd(fd) == lease.digest
        finally:
            os.close(fd)

    def close(self) -> list[str]:
        """Cierra los fds y sincroniza. Devuelve la lista de errores de cierre (JAMÁS los descarta: B193). El dir de
        cuarentena y su journal se PRESERVAN (move-only; no se borra en verde)."""
        errs: list[str] = []
        if self._closed:
            return errs
        self._closed = True
        jfd = getattr(self, "_jfd", -1)
        if jfd >= 0:
            try:
                os.fsync(jfd)
                os.close(jfd)
            except OSError as exc:
                errs.append(f"cerrar journal de cuarentena: {exc}")
            self._jfd = -1
        fd = getattr(self, "fd", -1)
        if fd >= 0:
            try:
                os.fsync(fd)
                os.close(fd)
            except OSError as exc:
                errs.append(f"cerrar/sincronizar cuarentena: {exc}")
            self.fd = -1
        return errs

    def __enter__(self) -> GovernedQuarantine:
        return self

    def __exit__(self, *exc: object) -> None:
        errs = self.close()
        if errs and exc[0] is None:  # B193: si no hay ya una excepción en vuelo, los errores de cierre SE ELEVAN
            raise GovernedQuarantineError("; ".join(errs))


def _no_dup(pairs: list[tuple]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise GovernedQuarantineError(f"clave JSON duplicada en journal: {k!r}")
        out[k] = v
    return out
