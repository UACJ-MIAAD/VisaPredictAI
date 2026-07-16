#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74/B79..B118/B119-B127 — extraído del heredoc de
run_campaign_aq{,_tail}.sh). Se invoca desde ROOT (run-command fija cwd=root).

CAS ATÓMICO SIN VENTANA (R9.2R10, B121-B125): `os.replace`/`os.rename` NO son CAS — sobrescriben una colisión
en silencio y el patrón validar→`os.replace` deja morir una actualización concurrente en la ventana. Aquí toda
promoción/restauración/recuperación usa `tools/atomic_fs`:
- output AUSENTE: `rename_noreplace(temp→target)` — si un tercero lo creó en la ventana, `FileExistsError` y se
  ABORTA sin tocarlo (nunca se sobrescribe una creación concurrente).
- output PREEXISTENTE: `rename_exchange(temp↔target)` — intercambio atómico; tras él, `target` liga a `temp_fd`
  (nuestro contenido) y `temp_name` liga a `orig_fd` (el original desplazado). Si `temp_name` NO liga al
  original (un tercero había reemplazado el target), se DESHACE el intercambio (swap de vuelta) y se preserva
  la actualización concurrente en su ruta oficial; nada se destruye (el exchange nunca borra un inode).
- ROLLBACK restaura con `rename_exchange(temp_name↔target)` y VERIFICA que el objeto desplazado era EXACTAMENTE
  `temp_fd`; si no lo era (actualización concurrente), se deshace y se preserva — jamás se sobrescriben bytes
  viejos encima de una actualización concurrente.

CUARENTENA como JOURNAL DURABLE (B119/B120/B121/B126): la limpieza mueve temporales/desplazados a
`.merge-quarantine/<txid>/` con `rename_noreplace` (nunca `os.rename` — sin sobrescritura silenciosa de una
colisión). `MANIFEST.jsonl` se abre `O_CREAT|O_EXCL|O_NOFOLLOW` 0600 y se exige regular/UID/nlink==1/modo 0600
(un hardlink/symlink/manifiesto ajeno plantado se rechaza). Por movimiento: registro INTENT (escritura completa
+ `fsync`) ANTES del rename, y COMPLETED/FOREIGN_PRESERVED (+ `fsync`) DESPUÉS, con `fsync` de qtx/qroot/dir
fuente. Un fallo del manifiesto ⇒ `_QUARANTINE_FAILED` (prohibido `except OSError: pass`). La recolección
(reconciliar un INTENT sin COMPLETED tras un crash) vive en P2b.

Máquina de estados EXPLÍCITA (`_TxContext.phase`): LOADING → PREPARING → PROMOTING → {ROLLING_BACK |
COMMIT_REACHED} → CLEANING → CLOSED. Errores CLASIFICADOS por INVARIANTES (B127):
- `RollbackError`: rollback COMPLETO, todos los outputs reconciliados, sin errores → reintentar es SEGURO.
- `RollbackIncompleteError`: hay actualización concurrente, output irrecuperable, cuarentena fallida, journal
  incompleto o cierre que afecta durabilidad → NO reintentar automáticamente.
- `CommittedStateError`: el commit SÍ se cruzó y quedó un problema post-commit (autoridad durable, no reintentar).

Validación en dominio (`_ValidationError`, atrapada → rollback); `KeyboardInterrupt`/`SystemExit` propagan.
LEASES vivos hasta el commit: 8 mitades de entrada (B115), output previo (`orig_fd`, B114) y lock (`_LockGuard`,
B116), revalidados en cada checkpoint. GOBERNANZA DE RUTAS (B90): cadena `.`→reports→campaign/eval abierta
componente a componente `openat O_DIRECTORY|O_NOFOLLOW`, fd-relativa, identidad reverificada.

FAIL-CLOSED sobre el esquema REAL (B79/B80/B85): 8 mitades, 19 columnas en orden, `run_id` string único,
`table` coincidente, strings no vacíos, NaN real ≠ texto coercionado ≠ infinito, `secs` ≥ 0. Identidad de
campaña (B85): con `CAMPAIGN_ID` las 8 mitades deben llevarla; standalone conserva el máximo lexicográfico.

Garantías honestas: validación GLOBAL previa; promoción/rollback ATÓMICOS por fichero vía CAS; durabilidad por
`fsync`. NO es atomicidad de bundle crash-safe (un kill a mitad puede dejar un INTENT sin COMPLETED, reconciliable
en P2b).
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import math
import os
import secrets
import stat
import sys
from typing import NoReturn

import pandas as pd

from tools.atomic_fs import AtomicRenameError, AtomicUnsupportedError, rename_exchange, rename_noreplace
from tools.governed_read import digest_fd, lease_problem, open_governed_lease, snapshot_fd

_TABLES = ("FAD", "DFF")
_BLOCKS = ("family", "employment")
_HALVES = ("nongbm", "gbm")
_POOL_COLS = (
    "run_id", "model", "country", "category", "table",
    "sel_mase", "sel_smape", "sel_mae", "sel_rmse",
    "hold_mase", "hold_smape", "hold_mae", "hold_rmse", "hold_msis", "hold_interval_score", "hold_coverage",
    "sel_mase1", "hold_mase1", "secs",
)  # fmt: skip
_STR_COLS = ("model", "country", "category", "table")
_METRIC_COLS = tuple(c for c in _POOL_COLS if c not in ("run_id", *_STR_COLS))
_LOCK_NAME = ".merge.lock"
_QUARANTINE_DIR = ".merge-quarantine"
_MANIFEST_NAME = "MANIFEST.jsonl"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Fases explícitas de la transacción.
_LOADING = "LOADING"
_PREPARING = "PREPARING"
_PROMOTING = "PROMOTING"
_ROLLING_BACK = "ROLLING_BACK"
_COMMIT_REACHED = "COMMIT_REACHED"
_CLEANING = "CLEANING"
_CLOSED = "CLOSED"


def _fail(msg: str) -> NoReturn:
    print(f"merge_campaign_pools: {msg}", file=sys.stderr)
    raise SystemExit(1)


class _ValidationError(Exception):
    """Violación de invariante DENTRO de la transacción — fallo de dominio ORDINARIO (atrapado → rollback),
    a diferencia de `KeyboardInterrupt`/`SystemExit` que propagan."""


class RollbackError(OSError):
    """B104/B127: fallo ANTES del commit y rollback COMPLETO — todos los outputs quedaron reconciliados (bytes
    originales restaurados o actualización concurrente preservada, sin residuo). Reintentar es SEGURO."""


class RollbackIncompleteError(OSError):
    """B127: fallo ANTES del commit pero el rollback NO pudo reconciliar todo — actualización concurrente que
    no se pudo restaurar limpiamente, output irrecuperable, cuarentena/journal fallidos o cierre que afecta la
    durabilidad. NO reintentar automáticamente: requiere inspección (posible estado en disco inconsistente)."""


class CommittedStateError(RuntimeError):
    """B104/B110: el commit SÍ se cruzó — los outputs nuevos son la AUTORIDAD y son durables — pero quedó estado
    incompleto (limpieza/fsync/cierre fallido o excepción posterior). Reintentar a ciegas es incorrecto."""


def _write_all(fd: int, data: bytes) -> None:
    """Escritura COMPLETA a `fd` (B126): una escritura parcial es un error, no un registro truncado."""
    mv = memoryview(data)
    off = 0
    while off < len(mv):
        n = os.write(fd, mv[off:])
        if n <= 0:
            raise OSError("escritura incompleta")
        off += n


def _open_dir_at(parent_fd: int | None, name: str, label: str) -> int:
    """B90: componente de la cadena gobernada. O_DIRECTORY|O_NOFOLLOW ⇒ un symlink revienta; el fstat del
    DESCRIPTOR exige dir real, del UID actual y sin escritura de grupo/otros."""
    try:
        fd = os.open(name, _DIR_FLAGS) if parent_fd is None else os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        _fail(f"directorio gobernado {label!r} inabrible (symlink/ausente/no-dir: {exc})")
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or (stat.S_IMODE(st.st_mode) & 0o022):
        os.close(fd)
        _fail(f"directorio gobernado {label!r} ajeno o escribible por grupo/otros")
    return fd


class _Chain:
    """Cadena gobernada `.` → reports → campaign/eval. Los descriptores viven toda la transacción;
    `reverify()` re-camina la cadena FRESCA desde cwd y exige la MISMA identidad (st_dev, st_ino) por nivel."""

    def __init__(self) -> None:
        fds: list[int] = []
        try:
            fds.append(_open_dir_at(None, ".", "."))
            fds.append(_open_dir_at(fds[0], "reports", "reports"))
            fds.append(_open_dir_at(fds[1], "campaign", "reports/campaign"))
            fds.append(_open_dir_at(fds[1], "eval", "reports/eval"))
        except BaseException:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        self.dot, self.reports, self.camp, self.ev = fds

    def fds(self) -> tuple[int, int, int, int]:
        return (self.dot, self.reports, self.camp, self.ev)

    def idents(self) -> list[tuple[int, int]]:
        return [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in self.fds()]

    def close(self, errs: list[str] | None = None) -> None:
        """B113: un fallo cerrando cualquiera de los 4 descriptores de la cadena se REPORTA."""
        for label, fd in zip(("dot", "reports", "campaign", "eval"), self.fds(), strict=True):
            try:
                os.close(fd)
            except OSError as exc:
                if errs is not None:
                    errs.append(f"cerrar cadena {label}: {exc}")

    def reverify(self, when: str) -> None:
        """Una discrepancia de identidad es `_ValidationError` (dominio) — atrapada y clasificada."""
        fresh = _Chain()
        try:
            if fresh.idents() != self.idents():
                raise _ValidationError(f"la cadena reports/campaign|eval cambió de identidad ({when}) — swap")
        finally:
            fresh.close()


class _LockGuard:
    """B116: lock gobernado como LEASE. Captura fd + (dev,ino) del inode bloqueado tras el flock; `problem`
    exige que `.merge.lock` dentro de camp_fd siga ligado a ESE inode y el fd siga regular/UID/nlink==1/0600."""

    __slots__ = ("fd", "dev", "ino")

    def __init__(self, fd: int, dev: int, ino: int) -> None:
        self.fd = fd
        self.dev = dev
        self.ino = ino

    def problem(self, camp_fd: int) -> str | None:
        prob = _fd_governed(self.fd, mode=0o600)
        if prob is not None:
            return f"lock {prob}"
        try:
            stn = os.stat(_LOCK_NAME, dir_fd=camp_fd, follow_symlinks=False)
        except OSError as exc:
            return f"lock ausente/inaccesible ({exc})"
        if (stn.st_dev, stn.st_ino) != (self.dev, self.ino):
            return "el lock fue sustituido (unlink+recreate tras el flock; otro proceso podría tomarlo)"
        return None

    def revalidate(self, camp_fd: int, when: str) -> None:
        prob = self.problem(camp_fd)
        if prob is not None:
            raise _ValidationError(f"lock inválido ({when}): {prob}")


def _check_lock_fd(fd: int) -> None:
    st = os.fstat(fd)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.geteuid()
        or st.st_nlink != 1
        or stat.S_IMODE(st.st_mode) != 0o600
    ):
        os.close(fd)
        _fail("lock de merge no-regular/ajeno/hardlink/permisos")


def _acquire_lock(camp_fd: int) -> _LockGuard:
    """B89/B90/B116: lock RELATIVO al descriptor de campaign. fstat antes Y después del flock; se captura la
    identidad (dev,ino) del inode bloqueado para revalidar el lease en cada checkpoint pre-commit."""
    try:
        fd = os.open(_LOCK_NAME, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=camp_fd)
        os.fchmod(fd, 0o600)
    except FileExistsError:
        try:
            fd = os.open(_LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=camp_fd)
        except OSError as exc:
            _fail(f"lock de merge inabrible ({exc})")
    except OSError as exc:
        _fail(f"lock de merge no creable ({exc})")
    _check_lock_fd(fd)
    fcntl.flock(fd, fcntl.LOCK_EX)
    _check_lock_fd(fd)
    st = os.fstat(fd)
    return _LockGuard(fd, st.st_dev, st.st_ino)


def _fd_governed(fd: int, *, mode: int | None) -> str | None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        return "no-regular"
    if st.st_uid != os.geteuid():
        return "ajeno"
    if st.st_nlink != 1:
        return "hardlink"
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        return f"modo {oct(stat.S_IMODE(st.st_mode))} != {oct(mode)}"
    return None


def _binding_problem(dir_fd: int, name: str, fd: int, *, mode: int | None) -> str | None:
    """El NOMBRE dentro de dir_fd debe ligar al MISMO inode (dev/ino) que `fd` — y `fd` regular/UID/nlink==1
    (+ modo si se exige). Un mismatch = el nombre fue sustituido: jamás autoriza operar por él."""
    prob = _fd_governed(fd, mode=mode)
    if prob is not None:
        return prob
    try:
        stn = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        return f"nombre {name!r} ausente/inaccesible ({exc})"
    stf = os.fstat(fd)
    if (stn.st_dev, stn.st_ino) != (stf.st_dev, stf.st_ino):
        return f"nombre {name!r} ya no liga al descriptor creado (dev/ino distinto)"
    return None


def _binds(dir_fd: int, name: str, fd: int, *, mode: int | None = 0o600) -> bool:
    """True si `name` dentro de `dir_fd` liga (dev/ino) al inode de `fd` y `fd` cumple el gobierno. Guarda toda
    OSError (un fd inválido o un nombre ausente ⇒ False, nunca eleva)."""
    try:
        return _binding_problem(dir_fd, name, fd, mode=mode) is None
    except OSError:
        return False


def _create_governed(dir_fd: int, base: str, kind: str, i: int) -> tuple[str, int]:
    """Crea un artefacto con nombre de NONCE aleatorio + PID + índice vía `O_CREAT|O_EXCL|O_NOFOLLOW` 0600.
    Devuelve (name, fd r/w VIVO)."""
    name = f".{base}.{kind}.{os.getpid()}.{i}.{secrets.token_hex(8)}"
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    return name, fd


# --------------------------------------------------------------------------------------------------------
# Cuarentena (B117/B119/B120/B121/B126): journal durable; `rename_noreplace`, manifiesto O_EXCL, INTENT/COMPLETED.
# --------------------------------------------------------------------------------------------------------

_QUARANTINED = "QUARANTINED"  # nuestro objeto salió del árbol vivo a la cuarentena inventariada
_ALREADY_ABSENT = "ALREADY_ABSENT"
_FOREIGN_OBJECT_PRESERVED = "FOREIGN_OBJECT_PRESERVED"  # se movió/preservó un objeto ajeno (no se destruyó)
_QUARANTINE_FAILED = "QUARANTINE_FAILED"


class _MoveResult:
    __slots__ = ("status", "qtx", "qname")

    def __init__(self, status: str, qtx: int = -1, qname: str | None = None) -> None:
        self.status = status
        self.qtx = qtx
        self.qname = qname


class _Quarantine:
    """Gestor durable de `.merge-quarantine/<txid>/` por descriptor de directorio gobernado. Manifiesto O_EXCL
    0600 (rechaza hardlink/symlink/ajeno), registros INTENT+COMPLETED con `fsync`, movimientos por
    `rename_noreplace` (nunca `os.rename`). NUNCA borra: preserva. Un fallo del manifiesto ⇒ QUARANTINE_FAILED."""

    __slots__ = ("txid", "_qtx", "_qroot", "_manifests", "fds")

    def __init__(self, txid: str) -> None:
        self.txid = txid
        self._qtx: dict[int, int] = {}  # dir_fd -> fd del subdir <txid>
        self._qroot: dict[int, int] = {}  # dir_fd -> fd de .merge-quarantine
        self._manifests: dict[int, int] = {}  # dir_fd -> fd del MANIFEST.jsonl
        self.fds: list[int] = []

    def _prepare(self, dir_fd: int) -> tuple[int, int, int]:
        """Crea/valida `.merge-quarantine/<txid>/MANIFEST.jsonl` bajo `dir_fd` (B120). Devuelve (qroot, qtx,
        mfd) con modos EXACTOS (0700/0700/0600), fstat de tipo/UID/nlink; hardlink o ajeno revientan."""
        if dir_fd in self._qtx:
            return self._qroot[dir_fd], self._qtx[dir_fd], self._manifests[dir_fd]
        try:
            os.mkdir(_QUARANTINE_DIR, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
        qroot = os.open(_QUARANTINE_DIR, _DIR_FLAGS, dir_fd=dir_fd)
        self.fds.append(qroot)
        os.fchmod(qroot, 0o700)
        st = os.fstat(qroot)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise _ValidationError(f"{_QUARANTINE_DIR} ajeno/no-dir/modo != 0700")
        os.mkdir(self.txid, 0o700, dir_fd=qroot)  # nonce → EEXIST reventaría (bien)
        qtx = os.open(self.txid, _DIR_FLAGS, dir_fd=qroot)
        self.fds.append(qtx)
        os.fchmod(qtx, 0o700)
        stt = os.fstat(qtx)
        if not stat.S_ISDIR(stt.st_mode) or stt.st_uid != os.geteuid() or stat.S_IMODE(stt.st_mode) != 0o700:
            raise _ValidationError("subdir de cuarentena ajeno/no-dir/modo != 0700")
        mfd = os.open(_MANIFEST_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=qtx)
        self.fds.append(mfd)
        os.fchmod(mfd, 0o600)
        stm = os.fstat(mfd)
        if (
            not stat.S_ISREG(stm.st_mode)
            or stm.st_uid != os.geteuid()
            or stm.st_nlink != 1
            or stat.S_IMODE(stm.st_mode) != 0o600
        ):
            raise _ValidationError("MANIFEST.jsonl ajeno/no-regular/hardlink/modo != 0600")
        self._qroot[dir_fd] = qroot
        self._qtx[dir_fd] = qtx
        self._manifests[dir_fd] = mfd
        return qroot, qtx, mfd

    def _journal(self, mfd: int, rec: dict) -> bool:
        """Escribe un registro COMPLETO + `fsync` (B126). False (no eleva) si falla; el llamador lo trata como
        QUARANTINE_FAILED — nunca `except: pass`."""
        try:
            _write_all(mfd, (json.dumps(rec, sort_keys=True) + "\n").encode())
            os.fsync(mfd)
            return True
        except OSError:
            return False

    def move(self, o: _Out, name: str | None, fd: int, *, phase: str, reason: str) -> _MoveResult:
        """Mueve `name` a la cuarentena con `rename_noreplace` (B121) y journal durable (B126). QUARANTINED si
        liga a NUESTRO `fd`; FOREIGN_OBJECT_PRESERVED si movimos/preservamos un ajeno; ALREADY_ABSENT si no
        había nada; QUARANTINE_FAILED ante error de syscall/manifiesto. Nunca eleva por un fallo operativo."""
        if name is None or fd < 0:
            return _MoveResult(_ALREADY_ABSENT)
        try:
            os.stat(name, dir_fd=o.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _MoveResult(_ALREADY_ABSENT)
        except OSError:
            return _MoveResult(_QUARANTINE_FAILED)
        try:
            digest: str | None = digest_fd(fd)  # digest de NUESTRO inode (para el manifiesto), antes de mover
        except OSError:
            digest = None  # el digest es informativo en el manifiesto; su ausencia no aborta el movimiento
        try:
            _qroot, qtx, mfd = self._prepare(o.dir_fd)
        except OSError, _ValidationError:
            return _MoveResult(_QUARANTINE_FAILED)
        qname = f"{o.label}.{name.lstrip('.')}.{secrets.token_hex(6)}"
        if not self._journal(
            mfd,
            {
                "record": "INTENT",
                "orig_name": name,
                "quarantined_as": qname,
                "digest": digest,
                "phase": phase,
                "reason": reason,
            },
        ):
            return _MoveResult(_QUARANTINE_FAILED)  # B119: sin INTENT durable no se mueve nada
        try:
            rename_noreplace(o.dir_fd, name, qtx, qname)  # B121: nunca sobrescribe una colisión en el destino
        except FileNotFoundError:
            return _MoveResult(_ALREADY_ABSENT)
        except FileExistsError, AtomicRenameError, AtomicUnsupportedError, OSError:
            return _MoveResult(_QUARANTINE_FAILED)
        bound = _binds(qtx, qname, fd, mode=None)  # el objeto movido puede ser un temp (0600) o un original (0644)
        try:
            stq = os.stat(qname, dir_fd=qtx, follow_symlinks=False)
            inode = [stq.st_dev, stq.st_ino]
        except OSError:
            inode = None
        ok = self._journal(
            mfd,
            {
                "record": "COMPLETED" if bound else "FOREIGN_PRESERVED",
                "quarantined_as": qname,
                "inode": inode,
                "bound_to_tx_fd": bound,
            },
        )
        durable = self._fsync_all(o.dir_fd, qtx)  # B126: la durabilidad del movimiento se EXIGE, no es best-effort
        if not ok or not durable:
            return _MoveResult(_QUARANTINE_FAILED, qtx, qname)  # movido pero journal/durabilidad incompletos
        return _MoveResult(_QUARANTINED if bound else _FOREIGN_OBJECT_PRESERVED, qtx, qname)

    def restore(self, mr: _MoveResult, dst_dir_fd: int, dst: str) -> bool:
        """B125: devuelve un objeto ajeno de la cuarentena a su ruta oficial con `rename_noreplace`. True si la
        ruta oficial estaba libre y lo recibió; False si estaba ocupada (colisión) o error — se preserva en
        cuarentena y el llamador registra rollback incompleto."""
        if mr.qname is None or mr.qtx < 0:
            return False
        try:
            rename_noreplace(mr.qtx, mr.qname, dst_dir_fd, dst)
            return True
        except FileExistsError, FileNotFoundError, AtomicRenameError, AtomicUnsupportedError, OSError:
            return False

    def _fsync_all(self, src_dir_fd: int, qtx: int) -> bool:
        """B126: fsync de la fuente, el subdir de tx y la raíz de cuarentena. Devuelve False si alguno falla
        (NO se traga: el llamador degrada el movimiento a QUARANTINE_FAILED)."""
        ok = True
        for fd in (src_dir_fd, qtx, self._qroot.get(src_dir_fd, -1)):
            if fd >= 0:
                try:
                    os.fsync(fd)
                except OSError:
                    ok = False
        return ok

    def close(self, errs: list[str]) -> None:
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError as exc:
                errs.append(f"cerrar fd de cuarentena: {exc}")
        self.fds.clear()


class _Out:
    """Estado transaccional por output. Propiedad por descriptor (nonce+O_EXCL, fd vivo). `existed_before` +
    `orig_fd` (lease del inode previo, B114); tras el exchange de promoción el ORIGINAL vive en `temp_name`
    ligado a `orig_fd`. `incomplete` marca una reconciliación imposible (actualización concurrente, etc., B127)."""

    __slots__ = (
        "dir_fd", "label", "name", "df",
        "existed_before", "previous_bytes", "previous_digest",
        "orig_fd", "orig_snapshot", "orig_digest",
        "temp_created", "temp_name", "temp_fd", "temp_digest",
        "promoted", "restored", "concurrent_update", "incomplete",
        "recovery_created", "recovery_name", "recovery_fd", "recovery_digest",
    )  # fmt: skip

    def __init__(self, dir_fd: int, label: str, name: str, df: pd.DataFrame) -> None:
        self.dir_fd = dir_fd
        self.label = label
        self.name = name
        self.df = df
        self.existed_before = False
        self.previous_bytes: bytes | None = None
        self.previous_digest: str | None = None
        self.orig_fd = -1
        self.orig_snapshot: tuple[int, ...] | None = None
        self.orig_digest: str | None = None
        self.temp_created = False
        self.temp_name: str | None = None
        self.temp_fd = -1
        self.temp_digest: str | None = None
        self.promoted = False
        self.restored = False
        self.concurrent_update = False
        self.incomplete = False
        self.recovery_created = False
        self.recovery_name: str | None = None
        self.recovery_fd = -1
        self.recovery_digest: str | None = None

    def close_fds(self, errs: list[str]) -> None:
        for fd_attr in ("recovery_fd", "temp_fd", "orig_fd"):
            fd = getattr(self, fd_attr)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as exc:
                    errs.append(f"cerrar {fd_attr} de {self.name!r}: {exc}")
                setattr(self, fd_attr, -1)


class _InputLease:
    """B115: mitad de entrada LEASED — fd vivo + snapshot + digest + DataFrame parseado de ESOS mismos bytes.
    Se revalida (nombre↔inode, snapshot, digest) en cada checkpoint hasta el commit."""

    __slots__ = ("dir_fd", "name", "fd", "snapshot", "digest", "df")

    def __init__(
        self, dir_fd: int, name: str, fd: int, snapshot: tuple[int, ...], digest: str, df: pd.DataFrame
    ) -> None:
        self.dir_fd = dir_fd
        self.name = name
        self.fd = fd
        self.snapshot = snapshot
        self.digest = digest
        self.df = df

    def problem(self) -> str | None:
        return lease_problem(self.dir_fd, self.name, self.fd, self.snapshot, self.digest)

    def close(self, errs: list[str]) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError as exc:
                errs.append(f"cerrar lease de entrada {self.name!r}: {exc}")
            self.fd = -1


def _lease_half(camp_fd: int, fname: str, table: str, campaign: str | None) -> _InputLease:
    """B115: abre la mitad como LEASE (fd vivo), lee sus bytes del MISMO descriptor con snapshot pre/post,
    parsea el DataFrame de esos bytes y valida el esquema. Fallo de validación = `_fail` (pre-transacción)."""
    fd, snap0, err = open_governed_lease(camp_fd, fname)
    if err is not None:
        _fail(f"mitad {fname!r}: {err}")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        data = b"".join(chunks)
        if snapshot_fd(fd) != snap0:
            _fail(f"mitad {fname!r}: mutada durante la lectura (snapshot fstat pre/post distinto)")
        digest = hashlib.sha256(data).hexdigest()
        df = pd.read_csv(io.BytesIO(data), dtype={"run_id": str})
        _validate_half(df, fname, table, campaign)
    except BaseException:
        os.close(fd)
        raise
    return _InputLease(camp_fd, fname, fd, snap0, digest, df)


def _validate_half(df: pd.DataFrame, fname: str, table: str, campaign: str | None) -> None:
    if df.empty:
        _fail(f"mitad vacía: {fname}")
    if tuple(df.columns) != _POOL_COLS:
        _fail(f"{fname} con columnas {list(df.columns)} != las 19 canónicas en orden")
    if df["run_id"].isna().any():
        _fail(f"run_id vacío en {fname}")
    rid = df["run_id"].astype(str)
    if (rid.str.strip() == "").any():
        _fail(f"run_id vacío en {fname}")
    if rid.nunique() != 1:
        _fail(f"{fname} con múltiples run_id ({rid.nunique()}) en una sola mitad")
    if campaign is not None and rid.iloc[0] != campaign:
        _fail(f"{fname} run_id {rid.iloc[0]!r} != CAMPAIGN_ID {campaign!r} (mezcla de campañas prohibida)")
    if (df["table"].astype(str) != table).any():
        _fail(f"{fname} columna table != {table} del nombre de fichero")
    for c in _STR_COLS:
        if df[c].isna().any():
            _fail(f"{fname} columna {c} con valores ausentes")
        if (df[c].astype(str).str.strip() == "").any():
            _fail(f"{fname} columna {c} con valores vacíos")
    for c in _METRIC_COLS:
        raw = df[c]
        v = pd.to_numeric(raw, errors="coerce")
        if (raw.notna() & v.isna()).any():
            _fail(f"{fname} columna {c} con texto no numérico")
        if ((v == math.inf) | (v == -math.inf)).any():
            _fail(f"{fname} columna {c} con valor infinito")
    secs = pd.to_numeric(df["secs"], errors="coerce")
    if secs.isna().any() or (secs < 0).any():
        _fail(f"{fname} columna secs ausente o negativa")


class _TxContext:
    """Resultado transaccional compartido. CLASIFICA por INVARIANTES (B127): `incomplete` (rollback no
    reconciliado) se distingue de un rollback limpio. Un error secundario nunca reemplaza `primary_error`."""

    __slots__ = ("phase", "commit_reached", "incomplete", "primary_error", "rollback_errors", "recoveries", "postcommit_errors", "close_errors")  # fmt: skip

    def __init__(self) -> None:
        self.phase = _LOADING
        self.commit_reached = False
        self.incomplete = False  # B127: el rollback no pudo reconciliar (concurrencia/irrecuperable/journal)
        self.primary_error: BaseException | None = None
        self.rollback_errors: list[str] = []
        self.recoveries: list[str] = []
        self.postcommit_errors: list[str] = []
        self.close_errors: list[str] = []


def _recover_from_bytes(o: _Out, ctx: _TxContext) -> None:
    """B124/B118: recuperación TOTAL desde `previous_bytes` con CAS — un guard externo captura CUALQUIER
    Exception (jamás interrumpe el rollback global)."""
    try:
        _recover_from_bytes_inner(o, ctx)
    except Exception as exc:  # noqa: BLE001 — la recuperación nunca escapa
        ctx.rollback_errors.append(f"recuperación de {o.name!r} abortó ({type(exc).__name__}: {exc})")
        o.incomplete = True
        ctx.incomplete = True


def _recover_from_bytes_inner(o: _Out, ctx: _TxContext) -> None:
    errs = ctx.rollback_errors
    if o.previous_bytes is None or o.previous_digest is None:
        errs.append(f"sin bytes previos de confianza para recuperar {o.name!r}")
        o.incomplete = True
        ctx.incomplete = True
        return
    try:
        o.recovery_name, o.recovery_fd = _create_governed(o.dir_fd, o.name, "rec", 0)
        o.recovery_created = True
        with os.fdopen(o.recovery_fd, "wb", closefd=False) as rf:
            rf.write(o.previous_bytes)
            rf.flush()
            os.fsync(rf.fileno())
        o.recovery_digest = digest_fd(o.recovery_fd)
    except OSError as exc:
        errs.append(f"materializar recuperación de {o.name!r}: {exc}")
        o.incomplete = True
        ctx.incomplete = True
        return
    if o.recovery_digest != o.previous_digest or not _binds(o.dir_fd, o.recovery_name, o.recovery_fd):
        errs.append(f"recuperación de {o.name!r} no verifica antes de instalar")
        o.incomplete = True
        ctx.incomplete = True
        return
    # Instala la recuperación por CAS. Si el target existe → exchange (verificando que lo desplazado era
    # NUESTRO temporal); si no existe → noreplace. Nunca sobrescribe una actualización concurrente (B124).
    target_present = True
    try:
        os.stat(o.name, dir_fd=o.dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        target_present = False
    except OSError as exc:
        errs.append(f"estado del target {o.name!r} en recuperación: {exc}")
        o.incomplete = True
        ctx.incomplete = True
        return
    if not target_present:
        try:
            rename_noreplace(o.dir_fd, o.recovery_name, o.dir_fd, o.name)
        except FileExistsError:
            errs.append(f"recuperación de {o.name!r}: apareció una creación concurrente, preservada")
            o.concurrent_update = True
            o.incomplete = True
            ctx.incomplete = True
            return
        if _binds(o.dir_fd, o.name, o.recovery_fd) and digest_fd(o.recovery_fd) == o.previous_digest:
            o.restored = True
            ctx.recoveries.append(f"RECUPERACIÓN reports/{o.label}/{o.name} desde bytes de confianza")
        else:
            errs.append(f"recuperación de {o.name!r} instalada no verifica")
            o.incomplete = True
            ctx.incomplete = True
        return
    # target presente → sólo se sobrescribe si es NUESTRO temporal; si no, es una actualización concurrente.
    if not _binds(o.dir_fd, o.name, o.temp_fd):
        errs.append(
            f"{o.name!r} tiene una actualización concurrente; recuperación NO instalada, concurrente preservada"
        )
        o.concurrent_update = True
        o.incomplete = True
        ctx.incomplete = True
        return
    try:
        rename_exchange(o.dir_fd, o.recovery_name, o.dir_fd, o.name)
    except OSError as exc:
        errs.append(f"exchange de recuperación de {o.name!r}: {exc}")
        o.incomplete = True
        ctx.incomplete = True
        return
    # tras el exchange: target debe ligar a recovery_fd; lo desplazado (recovery_name) debe ser el temporal.
    if _binds(o.dir_fd, o.name, o.recovery_fd) and _binds(o.dir_fd, o.recovery_name, o.temp_fd):
        o.restored = True
        ctx.recoveries.append(f"RECUPERACIÓN reports/{o.label}/{o.name} desde bytes de confianza (CAS)")
    else:
        try:  # deshace: lo desplazado no era nuestro temporal → actualización concurrente, preservar
            rename_exchange(o.dir_fd, o.recovery_name, o.dir_fd, o.name)
        except OSError as exc:
            errs.append(f"no se pudo deshacer el exchange de recuperación de {o.name!r}: {exc}")
        errs.append(f"{o.name!r}: actualización concurrente durante la recuperación, preservada")
        o.concurrent_update = True
        o.incomplete = True
        ctx.incomplete = True


def _cas_promote(o: _Out) -> None:
    """Promoción por CAS (B122). Ausente: `rename_noreplace` (una creación concurrente ⇒ abort sin tocarla).
    Preexistente: `rename_exchange` — tras él `target`↔`temp_fd` y `temp_name`↔`orig_fd`; si el desplazado no
    es el original (un tercero reemplazó el target), se DESHACE y se preserva la concurrente. Sin ventana."""
    assert o.temp_name is not None
    if not o.existed_before:
        try:
            rename_noreplace(o.dir_fd, o.temp_name, o.dir_fd, o.name)
        except FileExistsError as exc:
            raise _ValidationError(f"output {o.name!r} ausente fue CREADO por un tercero; no se sobrescribe") from exc
        except (AtomicRenameError, AtomicUnsupportedError, OSError) as exc:
            raise _ValidationError(f"no se pudo promover {o.name!r} (noreplace): {exc}") from exc
        if not _binds(o.dir_fd, o.name, o.temp_fd):
            raise _ValidationError(f"output {o.name!r} tras promover no liga al temporal creado")
        o.promoted = True
        return
    try:
        rename_exchange(o.dir_fd, o.temp_name, o.dir_fd, o.name)
    except (AtomicRenameError, AtomicUnsupportedError, OSError) as exc:
        raise _ValidationError(f"no se pudo promover {o.name!r} (exchange): {exc}") from exc
    good = _binds(o.dir_fd, o.name, o.temp_fd) and _binds(o.dir_fd, o.temp_name, o.orig_fd, mode=None)
    if good:
        o.promoted = True
        return
    # el desplazado no es el original ⇒ un tercero reemplazó el target: deshaz el swap y preserva la concurrente
    try:
        rename_exchange(o.dir_fd, o.temp_name, o.dir_fd, o.name)
    except OSError as exc:
        raise _ValidationError(f"output {o.name!r} con actualización concurrente; no se pudo deshacer: {exc}") from exc
    raise _ValidationError(f"output {o.name!r} fue modificado por un tercero antes de promover; preservado")


def _cas_restore(o: _Out, ctx: _TxContext) -> None:
    """Rollback de un output PREEXISTENTE promovido. El original vive en `temp_name` (ligado a `orig_fd`) tras
    el exchange de promoción; se restaura con `rename_exchange(temp_name↔target)` VERIFICANDO que lo desplazado
    era EXACTAMENTE nuestro temporal (B123). Si el target traía una actualización concurrente, se deshace y se
    preserva; nunca se sobrescriben bytes viejos encima de una actualización concurrente."""
    errs = ctx.rollback_errors
    if o.temp_name is None or not _binds(o.dir_fd, o.temp_name, o.orig_fd, mode=None):
        _recover_from_bytes(o, ctx)  # el original no está donde debía → recupera desde bytes de confianza
        return
    try:
        rename_exchange(o.dir_fd, o.temp_name, o.dir_fd, o.name)
    except OSError as exc:
        errs.append(f"exchange de restauración de {o.name!r}: {exc}")
        _recover_from_bytes(o, ctx)
        return
    if _binds(o.dir_fd, o.name, o.orig_fd, mode=None) and _binds(o.dir_fd, o.temp_name, o.temp_fd):
        if digest_fd(o.orig_fd) == o.orig_digest:
            o.restored = True
            return
        errs.append(f"restauración de {o.name!r} con digest del original alterado")
        o.incomplete = True
        ctx.incomplete = True
        return
    # lo desplazado a temp_name NO es nuestro temporal ⇒ el target traía una actualización concurrente
    try:
        rename_exchange(o.dir_fd, o.temp_name, o.dir_fd, o.name)  # deshaz: concurrente vuelve al target
    except OSError as exc:
        errs.append(f"no se pudo deshacer la restauración de {o.name!r}: {exc}")
    errs.append(f"{o.name!r}: actualización concurrente tras promover; NO se restauran bytes viejos, preservada")
    o.concurrent_update = True
    o.incomplete = True
    ctx.incomplete = True


def _promote_transactionally(
    chain: _Chain, lock: _LockGuard, inputs: list[_InputLease], outs: list[_Out], quar: _Quarantine, ctx: _TxContext
) -> None:
    """Transacción con CAS atómico, leases y estado clasificable. NUNCA deja escapar un error operativo: todo
    se recopila en `ctx` y se clasifica en `_raise_outcome`."""

    def _fsync_dirs() -> None:
        os.fsync(chain.camp)
        os.fsync(chain.ev)

    def _revalidate_leases(when: str) -> None:
        for lease in inputs:  # B115
            prob = lease.problem()
            if prob is not None:
                raise _ValidationError(f"mitad de entrada inválida ({when}): {prob}")
        lock.revalidate(chain.camp, when)  # B116
        chain.reverify(when)  # B90

    def _quarantine_temp(o: _Out, reason: str) -> None:
        """Pone en cuarentena el temporal SOLO si `temp_name` sigue ligado a NUESTRO contenido (`temp_fd`); si
        liga al original (caso incompleto) o desapareció, no se toca. Consume el resultado (B112)."""
        if o.temp_name is None or o.temp_fd < 0 or not _binds(o.dir_fd, o.temp_name, o.temp_fd):
            return
        mr = quar.move(o, o.temp_name, o.temp_fd, phase=ctx.phase, reason=reason)
        if mr.status == _FOREIGN_OBJECT_PRESERVED:
            ctx.rollback_errors.append(f"temporal de {o.name!r} sustituido por objeto ajeno (preservado)")
        elif mr.status == _QUARANTINE_FAILED:
            ctx.rollback_errors.append(f"no se pudo poner en cuarentena el temporal de {o.name!r}")
            ctx.incomplete = True

    def _rollback_one(o: _Out) -> None:  # B111: contención total por output; jamás interrumpe a los demás
        try:
            if not o.promoted:
                return
            if not o.existed_before:  # ausente: deshaz nuestra creación por cuarentena (B125)
                mr = quar.move(o, o.name, o.temp_fd, phase=_ROLLING_BACK, reason="new-output-undo")
                if mr.status == _FOREIGN_OBJECT_PRESERVED:  # movimos una actualización concurrente → devuélvela
                    if quar.restore(mr, o.dir_fd, o.name):
                        o.concurrent_update = True
                        ctx.rollback_errors.append(f"{o.name!r}: actualización concurrente devuelta a la ruta oficial")
                    else:
                        o.incomplete = True
                        ctx.incomplete = True
                        ctx.rollback_errors.append(f"{o.name!r}: concurrente en cuarentena, ruta oficial ocupada")
                elif mr.status == _QUARANTINE_FAILED:
                    o.incomplete = True
                    ctx.incomplete = True
                    ctx.rollback_errors.append(f"no se pudo revertir {o.name!r} (cuarentena falló)")
                else:
                    o.restored = True  # QUARANTINED/ALREADY_ABSENT: la ruta oficial quedó limpia
                return
            _cas_restore(o, ctx)  # preexistente promovido → restaura el original por CAS
        except Exception as exc:  # noqa: BLE001 — contención total (B111)
            ctx.rollback_errors.append(f"rollback de {o.name!r} abortó ({type(exc).__name__}: {exc})")
            o.incomplete = True
            ctx.incomplete = True

    def _rollback() -> None:  # B106/B111/B112: NO eleva; contiene cada output; consume CADA resultado de limpieza
        ctx.phase = _ROLLING_BACK
        for o in reversed(outs):  # deshace en orden INVERSO
            _rollback_one(o)
        for o in outs:  # los temporales que aún tengan NUESTRO contenido → cuarentena (los originales se preservan)
            _quarantine_temp(o, "temp-cleanup")
        try:
            _fsync_dirs()  # B92
        except OSError as exc:
            ctx.rollback_errors.append(f"fsync de directorios: {exc}")

    try:
        ctx.phase = _PREPARING
        for i, o in enumerate(outs):  # 1. temporales: O_EXCL → escribe → fsync → digest
            data = o.df.to_csv(index=False).encode()
            o.temp_name, o.temp_fd = _create_governed(o.dir_fd, o.name, "tmp", i)
            o.temp_created = True
            with os.fdopen(o.temp_fd, "wb", closefd=False) as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            o.temp_digest = digest_fd(o.temp_fd)
        for (
            o
        ) in outs:  # 2. lease del previo (B114) + bytes de confianza (B108); SIN backup escrito (el exchange desplaza)
            fd, snap0, err = open_governed_lease(o.dir_fd, o.name)
            if err is not None and err.startswith("ausente"):
                o.existed_before = False
                continue
            if err is not None:
                raise _ValidationError(f"output previo {o.name!r}: {err}")
            o.orig_fd, o.orig_snapshot = fd, snap0
            os.lseek(fd, 0, os.SEEK_SET)
            prev_chunks: list[bytes] = []
            while chunk := os.read(fd, 1 << 16):
                prev_chunks.append(chunk)
            prev = b"".join(prev_chunks)
            if snapshot_fd(fd) != snap0:
                raise _ValidationError(f"output previo {o.name!r} mutado durante la lectura del lease")
            o.existed_before = True
            o.previous_bytes = prev
            o.previous_digest = hashlib.sha256(prev).hexdigest()
            o.orig_digest = o.previous_digest
        _revalidate_leases("después de cargar")  # B115/B116/B90 (checkpoint tras cargar las ocho)
        for o in outs:  # 3. verifica binding + digest del temporal ANTES de promover (B102)
            assert o.temp_name is not None
            prob = _binding_problem(o.dir_fd, o.temp_name, o.temp_fd, mode=0o600)
            if prob is not None:
                raise _ValidationError(f"temporal de {o.name!r} comprometido antes de promover: {prob}")
            if digest_fd(o.temp_fd) != o.temp_digest:
                raise _ValidationError(f"temporal de {o.name!r} mutado antes de promover (digest)")
        for o in (
            outs
        ):  # 3b. B114: el target no cambió desde el snapshot (preexistente liga al lease; ausente sigue ausente)
            if o.existed_before:
                assert o.orig_snapshot is not None and o.orig_digest is not None
                lp = lease_problem(o.dir_fd, o.name, o.orig_fd, o.orig_snapshot, o.orig_digest)
                if lp is not None:
                    raise _ValidationError(f"output {o.name!r} modificado por un tercero desde el snapshot: {lp}")
            else:
                try:
                    os.stat(o.name, dir_fd=o.dir_fd, follow_symlinks=False)
                    raise _ValidationError(f"output {o.name!r} ausente al inicio fue CREADO por un tercero")
                except FileNotFoundError:
                    pass
        ctx.phase = _PROMOTING
        for o in outs:  # 4. promoción por CAS atómico (B122) — sin ventana validación→replace
            _cas_promote(o)
        for o in outs:  # 5. verifica que el target liga al temporal que creamos + digest (B102)
            prob = _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600)
            if prob is not None:
                raise _ValidationError(f"output {o.name!r} tras promover no liga al temporal creado: {prob}")
            if digest_fd(o.temp_fd) != o.temp_digest:
                raise _ValidationError(f"output {o.name!r} tras promover con digest distinto")
        _fsync_dirs()
        _revalidate_leases("punto de commit")  # B99/B115/B116
        for o in outs:  # 6. B107: re-verifica los 8 target (binding + digest + modo) JUSTO antes del commit
            prob = _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600)
            if prob is not None:
                raise _ValidationError(f"output {o.name!r} alterado antes del commit: {prob}")
            if digest_fd(o.temp_fd) != o.temp_digest:
                raise _ValidationError(f"output {o.name!r} mutado antes del commit (digest) — contenido falsificado")
        ctx.commit_reached = True  # ---- PUNTO DE COMMIT ----
        ctx.phase = _CLEANING
        for o in outs:  # limpia los ORIGINALES desplazados (viven en temp_name para los preexistentes) por cuarentena
            if not (o.existed_before and o.temp_name is not None):
                continue
            try:
                os.stat(o.temp_name, dir_fd=o.dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue  # el original desplazado ya no está (consumido por una restauración que no ocurre aquí)
            except OSError as exc:
                ctx.postcommit_errors.append(f"estado del original desplazado de {o.name!r}: {exc}")
                continue
            # SIEMPRE se intenta mover (nada de saltar en silencio un temp_name sustituido, B105): `move` clasifica.
            mr = quar.move(o, o.temp_name, o.orig_fd, phase=_CLEANING, reason="post-commit-original-cleanup")
            if mr.status == _FOREIGN_OBJECT_PRESERVED:
                ctx.postcommit_errors.append(
                    f"original desplazado de {o.name!r} sustituido por objeto ajeno (preservado)"
                )
            elif mr.status == _QUARANTINE_FAILED:
                ctx.postcommit_errors.append(f"no se pudo poner en cuarentena el original desplazado de {o.name!r}")
        try:
            _fsync_dirs()
        except OSError as exc:
            ctx.postcommit_errors.append(f"fsync de durabilidad post-commit: {exc}")
    except Exception as primary:  # noqa: BLE001 — dominio/OSError sí; KeyboardInterrupt/SystemExit NO
        ctx.primary_error = primary
        if not ctx.commit_reached:
            _rollback()  # B106/B111: NO eleva


def _raise_outcome(ctx: _TxContext) -> None:
    """Clasifica por INVARIANTES (B127), no solo por `commit_reached`:
    - post-commit + cualquier problema (incl. `primary_error`, B110) ⇒ CommittedStateError;
    - pre-commit + rollback INCOMPLETO (concurrencia/irrecuperable/journal/cierre) ⇒ RollbackIncompleteError;
    - pre-commit + rollback completo con error primario ⇒ RollbackError;
    - éxito ⇒ retorna. Un error de cierre nunca reemplaza el primario: se ADJUNTA."""
    if ctx.commit_reached:
        problems: list[str] = []
        if ctx.primary_error is not None:  # B110
            problems.append(f"excepción post-commit: {ctx.primary_error!r}")
        problems += ctx.postcommit_errors
        problems += [f"cierre: {e}" for e in ctx.close_errors]
        if problems:
            raise CommittedStateError(
                f"COMMIT CRUZADO (outputs nuevos son la AUTORIDAD y son durables) pero quedó estado incompleto: "
                f"{problems}. NO reintentar como si hubiera rollback."
            )
        return  # éxito
    incomplete = ctx.incomplete or bool(ctx.close_errors)
    if ctx.primary_error is None and not ctx.rollback_errors and not incomplete:
        return  # nada que reportar
    detail = (
        f"{ctx.primary_error!r}; recuperaciones: {ctx.recoveries}; errores de rollback: {ctx.rollback_errors}; "
        f"cierres: {ctx.close_errors}"
    )
    if incomplete:  # B127: el rollback NO reconcilió todo → no reintentar automáticamente
        err_i = RollbackIncompleteError(f"ROLLBACK INCOMPLETO (no reintentar automáticamente): {detail}")
        if ctx.primary_error is not None:
            raise err_i from ctx.primary_error
        raise err_i
    if ctx.primary_error is None:
        raise RollbackError(f"errores de rollback sin error primario: {ctx.rollback_errors}")
    raise RollbackError(detail) from ctx.primary_error


def merge() -> int:
    campaign = os.environ.get("CAMPAIGN_ID")
    if campaign is not None and not campaign.strip():
        _fail("CAMPAIGN_ID definido pero vacío")
    chain = _Chain()  # B90: cadena gobernada abierta ANTES de tocar nada (cwd = ROOT)
    lock: _LockGuard | None = None
    inputs: list[_InputLease] = []
    outs: list[_Out] = []
    quar = _Quarantine(f"{os.getpid()}.{secrets.token_hex(8)}")
    ctx = _TxContext()
    try:
        lock = _acquire_lock(chain.camp)  # B89/B116: exclusión; los inputs se validan BAJO el lock
        try:
            chain.reverify("tras adquirir el lock")
        except _ValidationError as exc:
            _fail(str(exc))
        for table in _TABLES:  # 1. carga + valida las OCHO mitades como LEASES (B115), bajo el lock, fd-bound
            for block in _BLOCKS:
                halves = [
                    _lease_half(chain.camp, f"aq_pool_{kind}_{table}_{block}.csv", table, campaign) for kind in _HALVES
                ]
                inputs.extend(halves)
                full = pd.concat([h.df for h in halves], ignore_index=True)
                full["source_run_id"] = full["run_id"]
                full["run_id"] = campaign if campaign is not None else str(full["run_id"].astype(str).max())
                tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
                outs.append(_Out(chain.camp, "campaign", f"campaign_pool_{table}_{block}.csv", full))
                outs.append(_Out(chain.ev, "eval", tgt, full))
                print(f"{table}/{block}: {len(full)} rows -> {tgt}")
        _promote_transactionally(chain, lock, inputs, outs, quar, ctx)  # 2. CAS; `ctx` clasifica (nunca eleva)
    finally:  # cierre único de TODOS los descriptores; errores → `ctx.close_errors` (B109/B113)
        ctx.phase = _CLOSED
        for o in outs:
            o.close_fds(ctx.close_errors)
        for lease in inputs:
            lease.close(ctx.close_errors)
        quar.close(ctx.close_errors)
        if lock is not None and lock.fd >= 0:
            try:
                os.close(lock.fd)
            except OSError as exc:
                ctx.close_errors.append(f"cerrar lock: {exc}")
        chain.close(ctx.close_errors)
    _raise_outcome(ctx)  # B104/B109/B110/B127: clasifica por invariantes
    return 0


if __name__ == "__main__":
    sys.exit(merge())
