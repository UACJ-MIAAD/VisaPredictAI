#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74/B79..B127/B128-B139 — extraído del heredoc de
run_campaign_aq{,_tail}.sh). Se invoca desde ROOT (run-command fija cwd=root).

CAS ATÓMICO CON ESTADO EXPLÍCITO (R9.2R11, B129/B139): toda promoción/restauración/recuperación usa
`tools/atomic_fs` (`rename_noreplace`/`rename_exchange`). En cuanto un `rename_exchange` RETORNA, el output se
considera MODIFICADO (`exchange_applied=True`); a partir de ahí, si la verificación falla se intenta un
exchange COMPENSATORIO y SOLO una compensación VERIFICADA (ambos nombres, inodes y digests) limpia el estado.
Si la compensación falla, no verifica, o SE DETECTÓ una actualización concurrente (aunque se preserve
correctamente en su ruta oficial), el resultado es INCOMPLETO: hubo una divergencia externa que impide un
reintento automático seguro (`RollbackIncompleteError`, B139). Un output nunca queda `promoted=False` si su
primer exchange ocurrió y no fue compensado y verificado.

CUARENTENA = JOURNAL DURABLE BIDIRECCIONAL (B128/B136): la limpieza mueve objetos a `.merge-quarantine/<txid>/`
con `rename_noreplace` (nunca `os.rename`). `MANIFEST.jsonl` se abre `O_CREAT|O_EXCL|O_RDWR|O_APPEND|O_NOFOLLOW`
0600 (regular/UID/nlink==1/modo exacto). Cada operación (MOVE y RESTORE) escribe eventos INTENT y
COMPLETED/FOREIGN_PRESERVED/COLLISION/ABORTED con `schema_version`, `sequence`, `operation_id`, identidad
esperada y **cadena de hashes** (`previous_record_sha256`/`record_sha256`); tras cada escritura se hace `fsync`
y se RE-LEE el registro desde el MISMO fd validando esquema/secuencia/cadena — un manifiesto truncado o alterado
entre INTENT y COMPLETED aborta (B136). `restore()` también escribe RESTORE_INTENT/COMPLETED y hace `fsync`
(B128). El directorio de cuarentena preexistente NO se REPARA: si su modo no es exactamente 0700 se ABORTA
(B135); sólo un directorio creado en ESTA ejecución se crea 0700.

FRONTERA DEL COMMIT CON RECIBO GOBERNADO (B131/B132): entre la última revalidación y el commit no basta con
otra revalidación. Se revalidan inputs+lock+cadena+outputs, se escribe un RECIBO 0600 (identidad dev/ino y
digest de las 8 mitades y los 8 outputs, lock, cadena, manifiestos, fase, sha de código), se `fsync`, se
promueve con `rename_noreplace`, se `fsync` del directorio, se RE-ABRE desde la ruta oficial y se REVALIDA
contra los inputs/outputs actuales; SÓLO entonces se marca `commit_reached=True`. Un input/lock/directorio
cambiado tras la última revalidación se caza aquí.

Errores CLASIFICADOS por INVARIANTES con taxonomía ESTRUCTURADA (`Issue`, B127/B130/B139): cualquier `Issue`
de severidad "incomplete" (concurrencia detectada, compensación no verificada, `fsync` fallido, journal
inválido, restore sin certificar, cuarentena fallida, output no reconciliado, cierre fallido, recibo inválido)
⇒ `RollbackIncompleteError` (NO reintentar). Pre-commit + todo reconciliado ⇒ `RollbackError`. Post-commit +
cualquier problema ⇒ `CommittedStateError`. Commit certificado + cero problemas ⇒ éxito.

Validación en dominio (`_ValidationError`, atrapada → rollback); `KeyboardInterrupt`/`SystemExit` propagan.
LEASES vivos hasta el commit: 8 mitades (B115), output previo (`orig_fd`, B114), lock (`_LockGuard`, B116).
GOBERNANZA DE RUTAS (B90): cadena `.`→reports→campaign/eval `openat O_DIRECTORY|O_NOFOLLOW`, fd-relativa.
FAIL-CLOSED sobre el esquema REAL (B79/B80/B85): 8 mitades, 19 columnas, `run_id` string único, `table`
coincidente, NaN real ≠ texto ≠ infinito, `secs` ≥ 0, identidad de campaña `CAMPAIGN_ID`.
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
import subprocess
import sys
from typing import NoReturn

import pandas as pd

import tools.campaign_bundle as _bundle
from tools.atomic_fs import AtomicRenameError, AtomicUnsupportedError, rename_exchange, rename_noreplace
from tools.governed_read import digest_fd, lease_problem, open_governed_lease, relative_name_problem, snapshot_fd

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
_ABORTED_DIR = ".merge-aborted"
_MANIFEST_NAME = "MANIFEST.jsonl"
_RECEIPT_PREFIX = ".merge-receipt"
_SCHEMA_VERSION = 1
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Fases explícitas de la transacción.
_LOADING = "LOADING"
_PREPARING = "PREPARING"
_PROMOTING = "PROMOTING"
_CERTIFYING = "CERTIFYING"  # frontera del commit (recibo)
_ROLLING_BACK = "ROLLING_BACK"
_COMMIT_REACHED = "COMMIT_REACHED"
_CLEANING = "CLEANING"
_CLOSED = "CLOSED"

# Severidades de `Issue`.
_INCOMPLETE = "incomplete"  # ⇒ RollbackIncompleteError (pre-commit) o CommittedStateError (post-commit)
_NOTE = "note"  # informativo (p. ej. una recuperación confirmada)


def _fail(msg: str) -> NoReturn:
    print(f"merge_campaign_pools: {msg}", file=sys.stderr)
    raise SystemExit(1)


class _ValidationError(Exception):
    """Violación de invariante DENTRO de la transacción — fallo de dominio ORDINARIO (atrapado → rollback),
    a diferencia de `KeyboardInterrupt`/`SystemExit` que propagan."""


class RollbackError(OSError):
    """B104/B127: fallo ANTES del commit y rollback COMPLETO — todo reconciliado, SIN divergencia externa ni
    fallo de durabilidad. Reintentar es SEGURO."""


class RollbackIncompleteError(OSError):
    """B127/B130/B139: fallo ANTES del commit pero el rollback NO reconcilió limpiamente — actualización
    concurrente detectada (aunque se preserve), compensación no verificada, `fsync`/journal/cuarentena fallidos,
    restore sin certificar, output irrecuperable o cierre que afecta la durabilidad. NO reintentar
    automáticamente: hubo una divergencia externa (posible estado en disco no canónico)."""


class CommittedStateError(RuntimeError):
    """B104/B110: el commit SÍ se cruzó — los outputs nuevos son la AUTORIDAD y son durables — pero quedó estado
    incompleto (limpieza/fsync/cierre fallido o excepción posterior). Reintentar a ciegas es incorrecto."""


class Issue:
    """Taxonomía ESTRUCTURADA de un problema (R9.2R11 §6). `severity=='incomplete'` fuerza la clasificación a
    RollbackIncompleteError/CommittedStateError; nunca es una simple cadena suelta."""

    __slots__ = ("code", "phase", "severity", "output", "detail")

    def __init__(self, code: str, phase: str, severity: str, output: str | None, detail: str) -> None:
        self.code = code
        self.phase = phase
        self.severity = severity
        self.output = output
        self.detail = detail

    def __repr__(self) -> str:
        return f"{self.code}[{self.severity}] {self.phase} {self.output or '-'}: {self.detail}"


def _write_all(fd: int, data: bytes) -> None:
    """Escritura COMPLETA a `fd` (B126): una escritura parcial es un error, no un registro truncado."""
    mv = memoryview(data)
    off = 0
    while off < len(mv):
        n = os.write(fd, mv[off:])
        if n <= 0:
            raise OSError("escritura incompleta")
        off += n


def _canon(rec: dict) -> bytes:
    return json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()


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
    try:
        return _binding_problem(dir_fd, name, fd, mode=mode) is None
    except OSError:
        return False


def _inode(dir_fd: int, name: str) -> tuple[int, int] | None:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _dir_binds(parent_fd: int, name: str, dir_fd: int) -> bool:
    """Como `_binds` pero para DIRECTORIOS (el objeto es un dir, no un fichero regular): `name` bajo `parent_fd`
    liga (dev/ino) al inode de `dir_fd`, que es un dir real del UID actual."""
    try:
        stf = os.fstat(dir_fd)
        if not stat.S_ISDIR(stf.st_mode) or stf.st_uid != os.geteuid():
            return False
        stn = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return (stn.st_dev, stn.st_ino) == (stf.st_dev, stf.st_ino)
    except OSError:
        return False


def _create_governed(dir_fd: int, base: str, kind: str, i: int) -> tuple[str, int]:
    """Crea un artefacto con nombre de NONCE aleatorio + PID + índice vía `O_CREAT|O_EXCL|O_NOFOLLOW` 0600."""
    name = f".{base}.{kind}.{os.getpid()}.{i}.{secrets.token_hex(8)}"
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    return name, fd


# --------------------------------------------------------------------------------------------------------
# Cuarentena: journal durable BIDIRECCIONAL con cadena de hashes + re-lectura (B119/B120/B126/B128/B135/B136).
# --------------------------------------------------------------------------------------------------------

_QUARANTINED = "QUARANTINED"
_ALREADY_ABSENT = "ALREADY_ABSENT"
_FOREIGN_OBJECT_PRESERVED = "FOREIGN_OBJECT_PRESERVED"
_QUARANTINE_FAILED = "QUARANTINE_FAILED"


class _MoveResult:
    __slots__ = ("status", "qtx", "qname")

    def __init__(self, status: str, qtx: int = -1, qname: str | None = None) -> None:
        self.status = status
        self.qtx = qtx
        self.qname = qname


# B146: esquema EXACTO por tipo de evento del journal (campos requeridos además de los comunes; ni faltantes ni
# extra). El "record" desconocido, un campo de más o de menos, un schema_version/txid falso ⇒ journal inválido.
_JOURNAL_COMMON = frozenset(
    {"schema_version", "txid", "sequence", "source_dir", "record", "previous_record_sha256", "record_sha256"}
)
_JOURNAL_SCHEMAS: dict[str, frozenset[str]] = {
    "MOVE_INTENT": frozenset(
        {"operation_id", "source_name", "destination_name", "expected_dev", "expected_ino", "expected_digest"}
    ),  # fmt: skip
    "MOVE_COMPLETED": frozenset({"operation_id", "destination_name", "moved_dev", "moved_ino", "bound_to_tx_fd"}),
    "MOVE_FOREIGN_PRESERVED": frozenset(
        {"operation_id", "destination_name", "moved_dev", "moved_ino", "bound_to_tx_fd"}
    ),  # fmt: skip
    "MOVE_ABORTED": frozenset({"operation_id", "destination_name", "detail"}),
    "RESTORE_INTENT": frozenset({"operation_id", "source_name", "destination_name", "expected_dev", "expected_ino"}),
    "RESTORE_COMPLETED": frozenset({"operation_id", "destination_name"}),
    "RESTORE_COLLISION": frozenset({"operation_id", "destination_name", "detail"}),
}
_JOURNAL_INTENTS = {"MOVE_INTENT", "RESTORE_INTENT"}
# B154: cada intent liga a su conjunto EXACTO de terminales (un MOVE_INTENT no puede cerrar con RESTORE_*).
_INTENT_TERMINALS: dict[str, frozenset[str]] = {
    "MOVE_INTENT": frozenset({"MOVE_COMPLETED", "MOVE_FOREIGN_PRESERVED", "MOVE_ABORTED"}),
    "RESTORE_INTENT": frozenset({"RESTORE_COMPLETED", "RESTORE_COLLISION"}),
}
_HEX64 = frozenset("0123456789abcdef")


def _is_int(x: object) -> bool:
    return type(x) is int  # excluye bool (bool es subclase de int) — B151


def _is_hex(x: object, *, length: int | None = None) -> bool:
    return isinstance(x, str) and (length is None or len(x) == length) and bool(x) and all(c in _HEX64 for c in x)


def _valid_journal_types(rec: dict) -> bool:
    """B151: valida los TIPOS de cada campo del journal (no sólo su presencia). `True`/`False` para un entero,
    un inode string, un digest basura, un `operation_id` vacío o un `source_dir` falso ⇒ inválido."""
    if not _is_int(rec["schema_version"]) or not (_is_int(rec["sequence"]) and rec["sequence"] > 0):
        return False
    if rec["source_dir"] not in ("campaign", "eval"):
        return False
    if not (isinstance(rec["txid"], str) and rec["txid"]):
        return False
    if not (isinstance(rec["previous_record_sha256"], str) and (rec["previous_record_sha256"] == "" or _is_hex(rec["previous_record_sha256"], length=64))):  # fmt: skip
        return False
    if not _is_hex(rec["record_sha256"], length=64):
        return False
    if not _is_hex(rec["operation_id"], length=16):
        return False
    for k in ("expected_dev", "expected_ino", "moved_dev", "moved_ino"):
        if k in rec and not (_is_int(rec[k]) and rec[k] >= 0):
            return False
    if "expected_digest" in rec and rec["expected_digest"] is not None and not _is_hex(rec["expected_digest"], length=64):  # fmt: skip
        return False
    if "bound_to_tx_fd" in rec and not isinstance(rec["bound_to_tx_fd"], bool):
        return False
    for k in ("source_name", "destination_name"):
        if k in rec and (relative_name_problem(rec[k]) is not None):
            return False
    if "detail" in rec and not (isinstance(rec["detail"], str) and 0 < len(rec["detail"]) <= 4096):
        return False
    return True


def _no_dup_keys(pairs: list[tuple]) -> dict:
    """B146: `object_pairs_hook` que RECHAZA claves JSON duplicadas (json.loads por defecto se queda con la
    última en silencio — un atacante podría ocultar un campo)."""
    seen: dict = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"clave JSON duplicada en el journal: {k!r}")
        seen[k] = v
    return seen


def _strict_loads(line: bytes) -> dict:
    return json.loads(line, object_pairs_hook=_no_dup_keys)


class _JournalState:
    __slots__ = ("qroot", "qtx", "mfd", "seq", "prev_sha", "source_label")

    def __init__(self, qroot: int, qtx: int, mfd: int, source_label: str) -> None:
        self.qroot = qroot
        self.qtx = qtx
        self.mfd = mfd
        self.seq = 0
        self.prev_sha = ""
        self.source_label = source_label  # "campaign"|"eval": el journal exige source_dir concordante (B151)


class _Quarantine:
    """Gestor durable de `.merge-quarantine/<txid>/` por descriptor de directorio gobernado. Manifiesto
    `O_CREAT|O_EXCL|O_RDWR|O_APPEND|O_NOFOLLOW` 0600 con cadena de hashes; cada evento se `fsync`ea y RE-LEE
    (B136). El directorio preexistente NO se repara: modo != 0700 ⇒ abort (B135)."""

    __slots__ = ("txid", "_states", "fds")

    def __init__(self, txid: str) -> None:
        self.txid = txid
        self._states: dict[int, _JournalState] = {}
        self.fds: list[int] = []

    def _governed_subdir(self, name: str, parent_fd: int, *, created: bool) -> int:
        """Abre `name` bajo `parent_fd` y exige dir real/UID/modo EXACTO 0700. Un directorio PREEXISTENTE
        (`created=False`) con modo != 0700 se RECHAZA — NO se repara con fchmod (B135)."""
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        self.fds.append(fd)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise _ValidationError(f"cuarentena {name!r} ajena o no-dir")
        if created:
            os.fchmod(fd, 0o700)  # sólo un dir creado en ESTA ejecución se ajusta a 0700
            st = os.fstat(fd)
        if stat.S_IMODE(st.st_mode) != 0o700:  # un preexistente con otro modo NO se repara: se aborta
            raise _ValidationError(
                f"cuarentena {name!r} con modo {oct(stat.S_IMODE(st.st_mode))} != 0700 (no se repara)"
            )
        return fd

    def _prepare(self, dir_fd: int, source_label: str) -> _JournalState:
        if dir_fd in self._states:
            return self._states[dir_fd]
        try:
            os.mkdir(_QUARANTINE_DIR, 0o700, dir_fd=dir_fd)
            root_created = True
        except FileExistsError:
            root_created = False  # preexistente → se VALIDA, no se repara
        qroot = self._governed_subdir(_QUARANTINE_DIR, dir_fd, created=root_created)
        os.mkdir(self.txid, 0o700, dir_fd=qroot)  # nonce → EEXIST reventaría (bien): siempre creado aquí
        qtx = self._governed_subdir(self.txid, qroot, created=True)
        # Manifiesto O_EXCL (rechaza hardlink/symlink plantado) + O_RDWR|O_APPEND (para re-leer la cadena).
        mfd = os.open(
            _MANIFEST_NAME,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
            dir_fd=qtx,
        )
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
        state = _JournalState(qroot, qtx, mfd, source_label)
        self._states[dir_fd] = state
        return state

    def _manifest_bound(self, source_dir_fd: int, st: _JournalState) -> bool:
        """B142: la CADENA del manifiesto sigue ligada — `MANIFEST.jsonl`↔mfd (dev/ino/modo 0600/nlink==1) y
        source→qroot→qtx. Un desligado+recreado del manifiesto (escribir el fd huérfano) se caza."""
        return (
            _binds(st.qtx, _MANIFEST_NAME, st.mfd, mode=0o600)
            and _dir_binds(st.qroot, self.txid, st.qtx)
            and _dir_binds(source_dir_fd, _QUARANTINE_DIR, st.qroot)
        )

    def _journal(self, st: _JournalState, source_dir_fd: int, source_dir: str, rec: dict) -> bool:
        """Escribe un evento con cadena de hashes, `fsync` y RE-LECTURA validada (B136/B142/B146). False (no
        eleva) si cualquier paso falla — el llamador lo trata como fallo de journal (incompleto/QUARANTINE_FAILED)."""
        try:
            if not self._manifest_bound(source_dir_fd, st):  # B142: binding ANTES de escribir
                return False
            st.seq += 1
            full = {"schema_version": _SCHEMA_VERSION, "txid": self.txid, "sequence": st.seq, "source_dir": source_dir, **rec, "previous_record_sha256": st.prev_sha}  # fmt: skip
            rec_sha = hashlib.sha256(_canon(full)).hexdigest()
            full["record_sha256"] = rec_sha
            line = _canon(full) + b"\n"
            _write_all(st.mfd, line)
            os.fsync(st.mfd)
            if not self._reread_ok(st, st.seq, rec_sha):  # B136/B146: re-lee del MISMO fd y valida esquema/seq/cadena
                return False
            if not self._manifest_bound(source_dir_fd, st):  # B142: binding DESPUÉS de escribir
                return False
            st.prev_sha = rec_sha
            return True
        except OSError, ValueError:
            return False

    def _reread_ok(self, st: _JournalState, expect_seq: int, expect_sha: str) -> bool:
        """Re-lee el manifiesto ENTERO desde su fd (offset 0) y valida la cadena entera (B136/B146): esquema
        EXACTO por tipo de evento, `schema_version`/`txid`, secuencia 1..N, hashes encadenados, sin claves
        duplicadas, y la máquina de estados operation_id → un único terminal por intent, sin terminal huérfano."""
        cur = os.lseek(st.mfd, 0, os.SEEK_CUR)
        try:
            os.lseek(st.mfd, 0, os.SEEK_SET)
            raw = b""
            while chunk := os.read(st.mfd, 1 << 16):
                raw += chunk
        finally:
            os.lseek(st.mfd, cur, os.SEEK_SET)
        lines = raw.split(b"\n")
        if lines and lines[-1] == b"":
            lines = lines[:-1]
        if len(lines) != expect_seq:
            return False
        prev = ""
        ops: dict[str, tuple[str, int]] = {}  # operation_id → (record del intent, nº de terminales vistos)
        for i, line in enumerate(lines, start=1):
            try:
                rec = _strict_loads(line)  # B146: rechaza claves duplicadas
            except ValueError:
                return False
            record = rec.get("record")
            if record not in _JOURNAL_SCHEMAS:
                return False  # tipo de registro desconocido
            if set(rec.keys()) != _JOURNAL_COMMON | _JOURNAL_SCHEMAS[record]:
                return False  # campo faltante o adicional
            if not _valid_journal_types(rec):  # B151: TIPOS exactos (bool≠int, digest 64hex, op_id 16hex, …)
                return False
            if rec["schema_version"] != _SCHEMA_VERSION or rec["txid"] != self.txid:
                return False
            if rec["source_dir"] != st.source_label:  # B151: source_dir concordante con el descriptor esperado
                return False
            if rec["sequence"] != i or rec["previous_record_sha256"] != prev:
                return False
            claimed = rec["record_sha256"]
            body = {k: v for k, v in rec.items() if k != "record_sha256"}
            if hashlib.sha256(_canon(body)).hexdigest() != claimed:
                return False
            op = rec["operation_id"]
            if record in _JOURNAL_INTENTS:
                if op in ops:
                    return False  # intent duplicado para el mismo operation_id
                ops[op] = (record, 0)
            else:  # terminal: debe tener un intent previo, ser el ÚNICO y de la FAMILIA correcta (B154)
                if op not in ops or ops[op][1] != 0 or record not in _INTENT_TERMINALS[ops[op][0]]:
                    return False
                ops[op] = (ops[op][0], 1)
            prev = claimed
        return prev == expect_sha

    def _fsync_all(self, source_dir_fd: int, st: _JournalState) -> bool:
        ok = True
        for fd in (source_dir_fd, st.qtx, st.qroot):
            try:
                os.fsync(fd)
            except OSError:
                ok = False
        return ok

    def move(self, o: _Out, name: str | None, fd: int, *, phase: str, reason: str) -> _MoveResult:
        """Mueve `name` a la cuarentena con `rename_noreplace` (B121) y journal MOVE_* durable (B126/B136)."""
        if name is None or fd < 0:
            return _MoveResult(_ALREADY_ABSENT)
        if _inode(o.dir_fd, name) is None:
            return _MoveResult(_ALREADY_ABSENT)
        try:
            digest: str | None = digest_fd(fd)
        except OSError:
            digest = None  # el digest es informativo; su ausencia no aborta el movimiento
        try:
            st = self._prepare(o.dir_fd, o.label)
        except OSError, _ValidationError:
            return _MoveResult(_QUARANTINE_FAILED)
        if not _dir_binds(st.qroot, self.txid, st.qtx):  # binding del dir de cuarentena antes del move
            return _MoveResult(_QUARANTINE_FAILED)
        op = secrets.token_hex(8)
        qname = f"{o.label}.{name.lstrip('.')}.{secrets.token_hex(6)}"
        exp = _inode(o.dir_fd, name) or (0, 0)
        intent = {"record": "MOVE_INTENT", "operation_id": op, "source_name": name, "destination_name": qname, "expected_dev": exp[0], "expected_ino": exp[1], "expected_digest": digest}  # fmt: skip
        if not self._journal(st, o.dir_fd, o.label, intent):  # B119: sin INTENT durable no se mueve nada
            return _MoveResult(_QUARANTINE_FAILED)
        try:
            rename_noreplace(o.dir_fd, name, st.qtx, qname)  # B121
        except FileNotFoundError:
            self._journal(st, o.dir_fd, o.label, {"record": "MOVE_ABORTED", "operation_id": op, "destination_name": qname, "detail": "source ausente"})  # fmt: skip
            return _MoveResult(_ALREADY_ABSENT)
        except FileExistsError, AtomicRenameError, AtomicUnsupportedError, OSError, ValueError:
            self._journal(st, o.dir_fd, o.label, {"record": "MOVE_ABORTED", "operation_id": op, "destination_name": qname, "detail": "rename falló"})  # fmt: skip
            return _MoveResult(_QUARANTINE_FAILED, st.qtx, qname)
        bound = _binds(st.qtx, qname, fd, mode=None)
        moved = _inode(st.qtx, qname) or (0, 0)
        rec = "MOVE_COMPLETED" if bound else "MOVE_FOREIGN_PRESERVED"
        ok = self._journal(st, o.dir_fd, o.label, {"record": rec, "operation_id": op, "destination_name": qname, "moved_dev": moved[0], "moved_ino": moved[1], "bound_to_tx_fd": bound})  # fmt: skip
        durable = self._fsync_all(o.dir_fd, st)  # B126: durabilidad EXIGIDA
        if not ok or not durable or not _dir_binds(st.qroot, self.txid, st.qtx):
            return _MoveResult(_QUARANTINE_FAILED, st.qtx, qname)
        return _MoveResult(_QUARANTINED if bound else _FOREIGN_OBJECT_PRESERVED, st.qtx, qname)

    def restore(self, o: _Out, mr: _MoveResult, dst_dir_fd: int, dst: str) -> bool:
        """B125/B128: devuelve un objeto ajeno de la cuarentena a su ruta oficial con `rename_noreplace`,
        JOURNALIZANDO RESTORE_INTENT/COMPLETED/COLLISION y `fsync`. True sólo si llegó a la ruta oficial."""
        if mr.qname is None or mr.qtx < 0 or o.dir_fd not in self._states:
            return False
        st = self._states[o.dir_fd]
        op = secrets.token_hex(8)
        moved = _inode(mr.qtx, mr.qname) or (0, 0)
        if not self._journal(st, o.dir_fd, o.label, {"record": "RESTORE_INTENT", "operation_id": op, "source_name": mr.qname, "destination_name": dst, "expected_dev": moved[0], "expected_ino": moved[1]}):  # fmt: skip
            return False
        try:
            rename_noreplace(mr.qtx, mr.qname, dst_dir_fd, dst)
        except FileExistsError, FileNotFoundError, AtomicRenameError, AtomicUnsupportedError, OSError, ValueError:
            self._journal(st, o.dir_fd, o.label, {"record": "RESTORE_COLLISION", "operation_id": op, "destination_name": dst, "detail": "ruta oficial ocupada o error"})  # fmt: skip
            return False
        ok = self._journal(st, o.dir_fd, o.label, {"record": "RESTORE_COMPLETED", "operation_id": op, "destination_name": dst})  # fmt: skip
        durable = ok
        for fd in (dst_dir_fd, mr.qtx, st.qtx, st.qroot):
            try:
                os.fsync(fd)
            except OSError:
                durable = False
        return ok and durable

    def close(self, errs: list[str]) -> None:
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError as exc:
                errs.append(f"cerrar fd de cuarentena: {exc}")
        self.fds.clear()


class _Out:
    """Estado transaccional por output con ESTADO CAS EXPLÍCITO (R9.2R11 §2). Tras el exchange de promoción el
    ORIGINAL vive en `temp_name` (ligado a `orig_fd`). `exchange_applied` marca que el output fue MODIFICADO por
    un `rename_exchange` que aún no fue compensado y verificado; `incomplete` una reconciliación imposible."""

    __slots__ = (
        "dir_fd", "label", "name", "df",
        "existed_before", "previous_bytes", "previous_digest",
        "orig_fd", "orig_snapshot", "orig_digest",
        "temp_created", "temp_name", "temp_fd", "temp_digest",
        "promoted", "restored", "concurrent_update", "incomplete",
        "cas_started", "exchange_applied", "compensation_attempted", "compensation_verified",
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
        self.cas_started = False
        self.exchange_applied = False
        self.compensation_attempted = False
        self.compensation_verified = False
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
    """B115: mitad de entrada LEASED — fd vivo + snapshot + digest + DataFrame parseado de ESOS mismos bytes."""

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
    """Resultado transaccional con taxonomía ESTRUCTURADA (`issues`, R9.2R11 §6). `incomplete` es DERIVADO de
    los issues de severidad 'incomplete' — un fallo de durabilidad o una divergencia externa NO puede quedar
    como una cadena suelta que no cambie la clasificación."""

    __slots__ = ("phase", "commit_reached", "primary_error", "issues", "recoveries", "close_errors", "receipt_name", "receipt_fd")  # fmt: skip

    def __init__(self) -> None:
        self.phase = _LOADING
        self.commit_reached = False
        self.primary_error: BaseException | None = None
        self.issues: list[Issue] = []
        self.recoveries: list[str] = []
        self.close_errors: list[str] = []
        self.receipt_name: str | None = None  # B144: recibo publicado pero commit no cruzado → moverlo a ABORTED
        self.receipt_fd = -1  # B149: fd del recibo que creamos (se lee de AQUÍ, no reabriendo por nombre)

    def flag(self, code: str, detail: str, output: str | None = None) -> None:
        """Registra un Issue INCOMPLETO (fuerza RollbackIncompleteError/CommittedStateError)."""
        self.issues.append(Issue(code, self.phase, _INCOMPLETE, output, detail))

    def note(self, code: str, detail: str, output: str | None = None) -> None:
        self.issues.append(Issue(code, self.phase, _NOTE, output, detail))

    @property
    def incomplete(self) -> bool:
        return any(i.severity == _INCOMPLETE for i in self.issues)

    def incomplete_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == _INCOMPLETE]


def _capture_object(dir_fd: int, name: str) -> dict | None:
    """Captura la identidad COMPLETA (dev/ino/uid/modo/nlink/digest) del objeto en `name` — para verificar que
    el MISMO objeto concurrente sigue allí tras una compensación (B141). None si no se pudo abrir/leer."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        return {
            "dev": st.st_dev, "ino": st.st_ino, "uid": st.st_uid,
            "mode": stat.S_IMODE(st.st_mode), "nlink": st.st_nlink, "digest": digest_fd(fd),
        }  # fmt: skip
    except OSError:
        return None
    finally:
        os.close(fd)


def _object_matches(dir_fd: int, name: str, cap: dict) -> bool:
    """True si el objeto en `name` es EXACTAMENTE el capturado (dev/ino/uid/modo/nlink/digest idénticos)."""
    now = _capture_object(dir_fd, name)
    return now is not None and now == cap


def _compensate_both_sides(o: _Out, ctx: _TxContext, *, code: str) -> None:
    """B141: tras un exchange que desplazó un objeto CONCURRENTE (no el original) a `temp_name`, deshace el
    intercambio y verifica AMBOS lados — `temp_name`↔`temp_fd` (nuestro contenido y su digest) Y `o.name`↔el
    concurrente EXACTO capturado antes del swap (dev/ino/uid/modo/nlink/digest). Sólo entonces
    `compensation_verified=True`. Una sustitución del concurrente durante la compensación NO verifica ⇒
    INCOMPLETO (la ruta oficial quedaría con un objeto ajeno)."""
    o.compensation_attempted = True
    assert o.temp_name is not None
    concurrent = _capture_object(o.dir_fd, o.temp_name)  # el concurrente vive AHORA en temp_name (tras el 1er swap)
    try:
        rename_exchange(o.dir_fd, o.temp_name, o.dir_fd, o.name)
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        ctx.flag(code, f"{o.name!r}: compensación (swap de vuelta) falló: {exc}", o.name)
        o.concurrent_update = True
        o.incomplete = True
        return
    our_ok = _binds(o.dir_fd, o.temp_name, o.temp_fd, mode=0o600)
    try:
        our_ok = our_ok and digest_fd(o.temp_fd) == o.temp_digest
    except OSError:
        our_ok = False
    concurrent_ok = concurrent is not None and _object_matches(o.dir_fd, o.name, concurrent)
    if our_ok and concurrent_ok:  # `compensation_verified` describe SÓLO el estado físico, no borra la divergencia
        o.exchange_applied = False
        o.compensation_verified = True
    else:  # un lado no verifica (p.ej. el concurrente fue SUSTITUIDO durante la compensación)
        ctx.flag(code, f"{o.name!r}: compensación no verificable en ambos lados (concurrente sustituido)", o.name)
    # B153/B139: TODA concurrencia DETECTADA es incompleta — aunque ambos lados queden restaurados exactamente,
    # hubo una divergencia externa que impide el reintento automático seguro. Se registra SIEMPRE el Issue.
    ctx.flag("CONCURRENT_UPDATE_DETECTED", f"{o.name!r}: actualización concurrente detectada durante {code}", o.name)
    o.concurrent_update = True
    o.incomplete = True


def _recover_from_bytes(o: _Out, ctx: _TxContext) -> None:
    """B124/B118: recuperación TOTAL desde `previous_bytes` con CAS — un guard externo captura CUALQUIER
    Exception (jamás interrumpe el rollback global)."""
    try:
        _recover_from_bytes_inner(o, ctx)
    except Exception as exc:  # noqa: BLE001 — la recuperación nunca escapa
        ctx.flag("RECOVERY_ABORTED", f"recuperación abortó ({type(exc).__name__}: {exc})", o.name)
        o.incomplete = True


def _recover_from_bytes_inner(o: _Out, ctx: _TxContext) -> None:
    if o.previous_bytes is None or o.previous_digest is None:
        ctx.flag("RECOVERY_NO_TRUSTED_BYTES", f"sin bytes de confianza para {o.name!r}", o.name)
        o.incomplete = True
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
        ctx.flag("RECOVERY_MATERIALIZE_FAILED", f"materializar recuperación de {o.name!r}: {exc}", o.name)
        o.incomplete = True
        return
    if o.recovery_digest != o.previous_digest or not _binds(o.dir_fd, o.recovery_name, o.recovery_fd):
        ctx.flag("RECOVERY_UNVERIFIED", f"recuperación de {o.name!r} no verifica antes de instalar", o.name)
        o.incomplete = True
        return
    target_present = _inode(o.dir_fd, o.name) is not None
    if not target_present:
        try:
            rename_noreplace(o.dir_fd, o.recovery_name, o.dir_fd, o.name)
        except FileExistsError:
            ctx.flag("RECOVERY_CONCURRENT_CREATE", f"{o.name!r}: creación concurrente durante recuperación", o.name)
            o.concurrent_update = True
            o.incomplete = True
            return
        if _binds(o.dir_fd, o.name, o.recovery_fd) and digest_fd(o.recovery_fd) == o.previous_digest:
            o.restored = True
            ctx.recoveries.append(f"RECUPERACIÓN reports/{o.label}/{o.name} desde bytes de confianza")
        else:
            ctx.flag("RECOVERY_INSTALL_UNVERIFIED", f"recuperación de {o.name!r} instalada no verifica", o.name)
            o.incomplete = True
        return
    if not _binds(o.dir_fd, o.name, o.temp_fd):  # target presente pero NO es nuestro temporal → concurrente
        ctx.flag("RECOVERY_CONCURRENT_UPDATE", f"{o.name!r}: actualización concurrente; recuperación no instalada", o.name)  # fmt: skip
        o.concurrent_update = True
        o.incomplete = True
        return
    o.cas_started = True
    try:
        rename_exchange(o.dir_fd, o.recovery_name, o.dir_fd, o.name)
        o.exchange_applied = True
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        ctx.flag("RECOVERY_EXCHANGE_FAILED", f"exchange de recuperación de {o.name!r}: {exc}", o.name)
        o.incomplete = True
        return
    if _binds(o.dir_fd, o.name, o.recovery_fd) and _binds(o.dir_fd, o.recovery_name, o.temp_fd):
        o.exchange_applied = False
        o.restored = True
        ctx.recoveries.append(f"RECUPERACIÓN reports/{o.label}/{o.name} desde bytes de confianza (CAS)")
        return
    try:  # el desplazado no era nuestro temporal → concurrente: deshaz y preserva
        rename_exchange(o.dir_fd, o.recovery_name, o.dir_fd, o.name)
        if _binds(o.dir_fd, o.name, o.temp_fd):
            o.exchange_applied = False
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        ctx.flag("RECOVERY_COMPENSATION_FAILED", f"no se pudo deshacer el exchange de recuperación de {o.name!r}: {exc}", o.name)  # fmt: skip
    ctx.flag("RECOVERY_CONCURRENT_UPDATE", f"{o.name!r}: actualización concurrente durante la recuperación", o.name)
    o.concurrent_update = True
    o.incomplete = True


def _cas_promote(o: _Out, ctx: _TxContext) -> None:
    """Promoción por CAS (B122). Ausente: `rename_noreplace`. Preexistente: `rename_exchange` con estado
    explícito — tras el swap, `exchange_applied=True`; si el desplazado no es el original (concurrencia), se
    intenta compensar y SÓLO una compensación verificada limpia el estado (si no, se ELEVA para el rollback)."""
    assert o.temp_name is not None
    if not o.existed_before:
        try:
            rename_noreplace(o.dir_fd, o.temp_name, o.dir_fd, o.name)
        except FileExistsError as exc:
            raise _ValidationError(f"output {o.name!r} ausente fue CREADO por un tercero; no se sobrescribe") from exc
        except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
            raise _ValidationError(f"no se pudo promover {o.name!r} (noreplace): {exc}") from exc
        if not _binds(o.dir_fd, o.name, o.temp_fd):
            raise _ValidationError(f"output {o.name!r} tras promover no liga al temporal creado")
        o.promoted = True
        return
    o.cas_started = True
    try:
        rename_exchange(o.dir_fd, o.temp_name, o.dir_fd, o.name)
        o.exchange_applied = True
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        raise _ValidationError(f"no se pudo promover {o.name!r} (exchange): {exc}") from exc
    if _binds(o.dir_fd, o.name, o.temp_fd) and _binds(o.dir_fd, o.temp_name, o.orig_fd, mode=None):
        o.promoted = (
            True  # el original quedó desplazado en temp_name (exchange_applied sigue True hasta commit/rollback)
        )
        return
    # el desplazado no es el original ⇒ concurrencia: compensa (AMBOS lados, B141) y ELEVA para el rollback
    _compensate_both_sides(o, ctx, code="PROMOTE_CONCURRENT_UPDATE")
    raise _ValidationError(f"output {o.name!r} fue modificado por un tercero antes de promover; preservado")


def _cas_restore(o: _Out, ctx: _TxContext) -> None:
    """Rollback de un output PREEXISTENTE promovido. Restaura con `rename_exchange(temp_name↔target)`; el
    original vive en temp_name (ligado a orig_fd). Verifica que lo desplazado era EXACTAMENTE `temp_fd` (B123);
    si no (concurrencia), compensa (swap de vuelta), verifica y marca INCOMPLETO (B139: la divergencia externa
    impide reintento). Un exchange no compensado deja `exchange_applied=True` ⇒ incompleto."""
    if o.temp_name is None or not _binds(o.dir_fd, o.temp_name, o.orig_fd, mode=None):
        _recover_from_bytes(o, ctx)
        return
    o.cas_started = True
    try:
        rename_exchange(o.dir_fd, o.temp_name, o.dir_fd, o.name)
        o.exchange_applied = True
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        ctx.flag("RESTORE_EXCHANGE_FAILED", f"exchange de restauración de {o.name!r}: {exc}", o.name)
        _recover_from_bytes(o, ctx)
        return
    if _binds(o.dir_fd, o.name, o.orig_fd, mode=None) and _binds(o.dir_fd, o.temp_name, o.temp_fd):
        try:
            digest_ok = digest_fd(o.orig_fd) == o.orig_digest
        except OSError:
            digest_ok = False
        if digest_ok:
            o.exchange_applied = False
            o.restored = True
            return
        ctx.flag("RESTORE_DIGEST_MISMATCH", f"restauración de {o.name!r} con digest del original alterado", o.name)
        o.incomplete = True
        return
    # lo desplazado NO es nuestro temporal ⇒ el target traía una actualización concurrente: compensa (AMBOS
    # lados, B141) y preserva.
    _compensate_both_sides(o, ctx, code="RESTORE_CONCURRENT_UPDATE")


def _module_hash(mod_name: str) -> str:
    mod = sys.modules.get(mod_name)
    path = getattr(mod, "__file__", None)
    if path is None:
        return "unknown"
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return "unknown"


def _git(*args: str) -> str | None:
    """B152/A6: resuelve git mediante COMANDOS git (compatible con worktrees donde `.git` es un fichero), no
    leyendo `.git/HEAD` a mano. None si git no está disponible o el cwd no es un repo (p. ej. un tmp de test)."""
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, check=False)  # noqa: S603,S607
    except OSError, subprocess.SubprocessError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _provenance() -> dict:
    """B147/B152: procedencia COMPLETA — git HEAD/tree/dirty (vía comandos git), python, contrato, env_id/perfil/
    variante (inyectados por el orquestador) y hashes completos de los tres módulos gobernantes."""
    try:
        contract = hashlib.sha256(open("environments/execution_contract.json", "rb").read()).hexdigest()
    except OSError:
        contract = "unknown"  # el contrato puede no estar presente (p. ej. en un tmp de test); no aborta
    status = _git("status", "--porcelain")
    return {
        "git_head": _git("rev-parse", "HEAD"),
        "git_tree": _git("rev-parse", "HEAD^{tree}"),
        "git_dirty": None if status is None else (status != ""),
        "env_id": os.environ.get("VP_ENV_ID"),  # el orquestador python_env lo inyecta en una ejecución oficial
        "profile": os.environ.get("VP_ENV_PROFILE"),
        "variant": os.environ.get("VP_ENV_VARIANT") or None,
        "python": sys.version.split()[0],
        "contract_sha256": contract,
        "modules": {
            "merge_campaign_pools": _module_hash("tools.merge_campaign_pools"),
            "atomic_fs": _module_hash("tools.atomic_fs"),
            "governed_read": _module_hash("tools.governed_read"),
        },
    }


def _receipt_body(
    chain: _Chain, lock: _LockGuard, inputs: list[_InputLease], outs: list[_Out], quar: _Quarantine
) -> dict:
    """Recibo de commit gobernado (B131/B132/B140/B147): identidad + digest ACTUAL (re-leído, no cacheado) de
    inputs/outputs, lock, cadena, manifiestos y procedencia completa."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "txid": quar.txid,
        "provenance": _provenance(),  # B147: git/python/contrato/env_id/hashes completos de los módulos
        "chain": chain.idents(),
        # inode ACTUAL de `.merge.lock` (no el guardado en el guard): un unlink+recreate del lock durante la
        # certificación cambia el inode y la re-lectura del recibo lo caza (B132).
        "lock": list(_inode(chain.camp, _LOCK_NAME) or (0, 0)),
        # B140: el digest del input se RECALCULA del contenido ACTUAL (fd vivo), NO se reutiliza `i.digest`
        # cacheado — una mutación in-place durante la certificación cambia el digest y la re-lectura la caza.
        "inputs": [
            {"name": i.name, "inode": list(_inode(i.dir_fd, i.name) or (0, 0)), "digest": _safe_digest(i.fd)}
            for i in inputs
        ],
        "outputs": [
            {
                "name": o.name,
                "label": o.label,
                "inode": list(_inode(o.dir_fd, o.name) or (0, 0)),
                "digest": _safe_digest(o.temp_fd),
            }
            for o in outs
        ],  # fmt: skip
        "manifests": sorted(st.prev_sha for st in quar._states.values()),
    }


def _safe_digest(fd: int) -> str | None:
    try:
        return digest_fd(fd)
    except OSError:
        return None


def _require_official_provenance(prov: dict) -> None:
    """B152: en una ejecución OFICIAL (marcada por `VP_OFFICIAL_RUN=1`, que el orquestador `python_env` fija),
    la procedencia debe estar COMPLETA y fail-closed — `env_id` 64hex, perfil `runtime`, variante null, git HEAD
    y tree de 40hex, `git_dirty=false`, contrato y módulos de 64hex; prohibido None/unknown. Una ejecución no
    oficial (dev/test) no exige esto. Cierra 'ejecución oficial fuera de run-command'."""
    if os.environ.get("VP_OFFICIAL_RUN") != "1":
        return
    problems: list[str] = []
    if not _is_hex(prov.get("env_id"), length=64):
        problems.append("env_id no es 64hex")
    if prov.get("profile") != "runtime":
        problems.append("perfil != runtime")
    if prov.get("variant") is not None:
        problems.append("variante != null")
    if not _is_hex(prov.get("git_head"), length=40):
        problems.append("git_head no es 40hex")
    if not _is_hex(prov.get("git_tree"), length=40):
        problems.append("git_tree no es 40hex")
    if prov.get("git_dirty") is not False:
        problems.append("git_dirty != false")
    if not _is_hex(prov.get("contract_sha256"), length=64):
        problems.append("contract_sha256 no es 64hex")
    mods = prov.get("modules") or {}
    if set(mods) != {"merge_campaign_pools", "atomic_fs", "governed_read"} or not all(
        _is_hex(h, length=64) for h in mods.values()
    ):
        problems.append("hashes de módulos incompletos")
    if problems:
        raise _ValidationError(f"procedencia oficial incompleta (B152): {problems}")


def _open_governed_0700(parent_fd: int, name: str, *, created: bool) -> int:
    """Abre `name` bajo `parent_fd` exigiendo dir real/UID/modo EXACTO 0700. Un preexistente con otro modo se
    RECHAZA (B135, no se repara); sólo un dir creado en ESTA ejecución se ajusta a 0700."""
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise _ValidationError(f"{name!r} ajeno o no-dir")
        if created:
            os.fchmod(fd, 0o700)
            st = os.fstat(fd)
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise _ValidationError(f"{name!r} con modo {oct(stat.S_IMODE(st.st_mode))} != 0700 (no se repara)")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _abort_receipt(camp_fd: int, txid: str, ctx: _TxContext) -> None:
    """B150: mueve el recibo huérfano (publicado sin commit) a `.merge-aborted/<txid>/` GOBERNADO por
    `rename_noreplace` — nada de un nombre predecible `recibo+".ABORTED"` ni de ignorar `FileExistsError`. Una
    colisión preplantada del txid, un objeto ajeno o cualquier fallo dejan un Issue INCOMPLETO; se verifica que
    la ruta oficial ya NO contiene el recibo."""
    if ctx.receipt_name is None or _inode(camp_fd, ctx.receipt_name) is None:
        return  # nunca se publicó / ya ausente
    fds: list[int] = []
    try:
        try:
            os.mkdir(_ABORTED_DIR, 0o700, dir_fd=camp_fd)
            root_created = True
        except FileExistsError:
            root_created = False  # el contenedor puede preexistir → se VALIDA (no se repara)
        aroot = _open_governed_0700(camp_fd, _ABORTED_DIR, created=root_created)
        fds.append(aroot)
        os.mkdir(txid, 0o700, dir_fd=aroot)  # create-only: una colisión PREPLANTADA del txid revienta EEXIST (B150)
        atx = _open_governed_0700(aroot, txid, created=True)
        fds.append(atx)
        aname = f"receipt.{secrets.token_hex(8)}"
        rename_noreplace(camp_fd, ctx.receipt_name, atx, aname)
        for fd in (camp_fd, atx, aroot):
            os.fsync(fd)
        if _inode(camp_fd, ctx.receipt_name) is not None:  # la ruta oficial DEBE quedar sin recibo
            ctx.flag("RECEIPT_ABORT_INCOMPLETE", "el recibo sigue en la ruta oficial tras abortar")
    except (
        FileExistsError,
        FileNotFoundError,
        AtomicRenameError,
        AtomicUnsupportedError,
        OSError,
        ValueError,
        _ValidationError,
    ) as exc:  # fail-closed: NO se ignora ninguna colisión/objeto ajeno/fallo
        ctx.flag("RECEIPT_ABORT_FAILED", f"no se pudo abortar el recibo (fail-closed): {exc}")
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError as exc:
                ctx.close_errors.append(f"cerrar aborted fd: {exc}")


def _bundle_provenance(quar: _Quarantine) -> dict:
    """B164: procedencia oficial COMPLETA para el manifiesto del bundle, en la forma EXACTA que exige el esquema
    cerrado: HEAD de git, hashes de los CUATRO módulos de la ruta de confianza + el contrato de ejecución (None si
    ausente, no 'unknown'), y la cabeza terminal de cada journal de cuarentena."""
    try:
        ec: str | None = hashlib.sha256(open("environments/execution_contract.json", "rb").read()).hexdigest()
    except OSError:
        ec = None
    heads = {st.source_label: (st.prev_sha or None) for st in quar._states.values() if st.prev_sha}
    head = _git("rev-parse", "HEAD")
    return {
        # `__file__` (no `_module_hash`) porque el productor corre como `__main__` (python -m): su nombre lógico
        # `tools.merge_campaign_pools` NO está en sys.modules y devolvería 'unknown'; el esquema exige hex64.
        "git_head": head if (head and len(head) == 40) else None,  # B171: 40-hex o None (nunca comodín)
        "code_sha_merge_campaign_pools": _file_sha(__file__),
        "code_sha_campaign_bundle": _file_sha(_bundle.__file__),
        "code_sha_atomic_fs": _module_hash("tools.atomic_fs"),
        "code_sha_governed_read": _module_hash("tools.governed_read"),
        "code_sha_execution_contract": ec,
        "journal_heads": heads,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "profile": os.environ.get("VP_ENV_PROFILE"),
        "variant": os.environ.get("VP_ENV_VARIANT") or None,
    }


def _file_sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _seal_bytes_from_fd(fd: int, certified_digest: str | None, what: str) -> bytes:
    """B158: relee el contenido COMPLETO desde un fd CERTIFICADO revalidando digest + snapshot fstat pre/post. Si
    el contenido mutó entre la certificación (recibo) y el sellado del bundle, ABORTA — el bundle jamás sella un
    estado distinto del certificado."""
    s0 = snapshot_fd(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    data = b""
    while chunk := os.read(fd, 1 << 16):
        data += chunk
    if certified_digest is None or hashlib.sha256(data).hexdigest() != certified_digest or snapshot_fd(fd) != s0:
        raise _bundle.BundleValidationError(f"{what} mutó entre la certificación y el sellado del bundle (B158)")
    return data


def _publish_bundle(chain: _Chain, quar: _Quarantine, inputs: list[_InputLease], outs: list[_Out], campaign: str | None, ctx: _TxContext) -> None:  # fmt: skip
    """B148/B145 + B158/B164: tras el commit del recibo, RELEE cada output desde su fd CERTIFICADO revalidando el
    digest (aborta si mutó entre certificación y sellado, B158), lee los BYTES REALES de cada input desde su lease
    (tamaño/hash sin reconstruir con pandas, B164), sella todo en un bundle inmutable content-addressed y publica el
    puntero CURRENT por CAS = AUTORIDAD durable. Output-neutral. Un fallo aquí es post-commit ⇒ Issue."""
    try:
        seen: dict[str, _InputLease] = {}
        for i in inputs:
            seen.setdefault(i.name, i)
        input_meta = [{"name": i.name, "bytes": _seal_bytes_from_fd(i.fd, i.digest, f"input {i.name!r}")} for i in seen.values()]  # fmt: skip
        output_meta = [
            {
                "label": o.label,
                "name": o.name,
                "bytes": _seal_bytes_from_fd(o.temp_fd, o.temp_digest, f"output {o.name!r}"),
                "rows": int(len(o.df)),
                "cols": int(len(o.df.columns)),
            }  # fmt: skip
            for o in outs
        ]
        _bundle.build_and_commit(chain.camp, quar.txid, campaign, output_meta, input_meta, _bundle_provenance(quar))
    except (_bundle.BundleError, OSError, ValueError) as exc:
        ctx.flag("BUNDLE_PUBLISH_FAILED", f"no se pudo publicar el bundle/puntero CURRENT post-commit: {exc}")


def _certify_commit(
    chain: _Chain, lock: _LockGuard, inputs: list[_InputLease], outs: list[_Out], quar: _Quarantine, ctx: _TxContext
) -> None:
    """B131/B132: recibo de commit gobernado. Revalida inputs+lock+cadena+outputs, escribe el recibo 0600,
    `fsync`, lo promueve con `rename_noreplace`, `fsync` del dir, lo RE-ABRE desde la ruta oficial y lo REVALIDA
    contra el estado actual. `commit_reached=True` sólo lo marca ESTE finalizador. Una divergencia (input/lock/
    directorio/output cambiado tras la última revalidación) aborta con `_ValidationError`."""
    for lease in inputs:  # revalidación final de leases + lock + cadena
        prob = lease.problem()
        if prob is not None:
            raise _ValidationError(f"mitad de entrada inválida (certificación): {prob}")
    lock.revalidate(chain.camp, "certificación")
    chain.reverify("certificación")
    for o in outs:  # los 8 target deben ligar a nuestro temporal con su digest
        prob = _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600)
        if prob is not None:
            raise _ValidationError(f"output {o.name!r} alterado antes del commit: {prob}")
        if digest_fd(o.temp_fd) != o.temp_digest:
            raise _ValidationError(f"output {o.name!r} mutado antes del commit (digest)")
    body = _receipt_body(chain, lock, inputs, outs, quar)
    _require_official_provenance(body["provenance"])  # B152: una ejecución OFICIAL exige procedencia completa
    tmp_name, tmp_fd = _create_governed(chain.camp, _RECEIPT_PREFIX, "tmp", 0)
    receipt_name = f"{_RECEIPT_PREFIX}.{quar.txid}.json"
    ctx.receipt_name = receipt_name  # B144: el rollback lo moverá a un dir aborted si el commit no se cruza
    ctx.receipt_fd = tmp_fd  # B149: se LEE de ESTE fd (el que creamos), NO reabriendo por nombre; se cierra en merge()
    try:
        os.lseek(tmp_fd, 0, os.SEEK_SET)
        os.ftruncate(tmp_fd, 0)
        _write_all(tmp_fd, _canon(body))
        os.fsync(tmp_fd)
        rename_noreplace(chain.camp, tmp_name, chain.camp, receipt_name)
        os.fsync(chain.camp)
    except (AtomicRenameError, AtomicUnsupportedError, OSError, ValueError) as exc:
        raise _ValidationError(f"no se pudo escribir/promover el recibo de commit: {exc}") from exc
    # B149: NO se re-abre por nombre (un tercero podría sustituir el nombre por otro inode con los mismos bytes).
    # Se exige que `receipt_name` LIGUE al fd que CREAMOS (dev/ino) con identidad gobernada, y se lee de ese fd.
    st0 = os.fstat(tmp_fd)
    if (
        not stat.S_ISREG(st0.st_mode)
        or st0.st_uid != os.geteuid()
        or st0.st_nlink != 1
        or stat.S_IMODE(st0.st_mode) != 0o600
    ):
        raise _ValidationError("recibo de commit no-regular/ajeno/hardlink/modo != 0600 (B143)")
    prob = _binding_problem(chain.camp, receipt_name, tmp_fd, mode=0o600)
    if prob is not None:
        raise _ValidationError(f"recibo de commit sustituido por otro inode: {prob} (B149)")
    os.lseek(tmp_fd, 0, os.SEEK_SET)
    raw = b""
    while chunk := os.read(tmp_fd, 1 << 16):
        raw += chunk
    if os.fstat(tmp_fd).st_ino != st0.st_ino:  # B143: el inode del fd no cambia durante la lectura
        raise _ValidationError("recibo de commit mutado durante la lectura")
    # Se comparan BYTES canónicos (tuplas y listas serializan idéntico) — un input/lock/output cambiado difiere.
    if raw != _canon(body) or raw != _canon(_receipt_body(chain, lock, inputs, outs, quar)):
        raise _ValidationError("el recibo de commit ya no corresponde al estado actual (input/lock/output cambió)")
    ctx.commit_reached = True  # ---- PUNTO DE COMMIT (único sitio) ----


def _promote_transactionally(
    chain: _Chain,
    lock: _LockGuard,
    inputs: list[_InputLease],
    outs: list[_Out],
    quar: _Quarantine,
    ctx: _TxContext,
    campaign: str | None = None,  # fmt: skip
) -> None:
    """Transacción con CAS atómico de estado explícito, journal durable y recibo de commit. NUNCA deja escapar
    un error operativo: todo se recopila en `ctx` (Issues) y se clasifica en `_raise_outcome`."""

    def _fsync_dirs() -> bool:
        ok = True
        for fd in (chain.camp, chain.ev):
            try:
                os.fsync(fd)
            except OSError:
                ok = False
        return ok

    def _revalidate_leases(when: str) -> None:
        for lease in inputs:
            prob = lease.problem()
            if prob is not None:
                raise _ValidationError(f"mitad de entrada inválida ({when}): {prob}")
        lock.revalidate(chain.camp, when)
        chain.reverify(when)

    def _quarantine_temp(o: _Out) -> None:
        if o.temp_name is None or o.temp_fd < 0 or not _binds(o.dir_fd, o.temp_name, o.temp_fd):
            return
        mr = quar.move(o, o.temp_name, o.temp_fd, phase=ctx.phase, reason="temp-cleanup")
        if mr.status == _FOREIGN_OBJECT_PRESERVED:
            ctx.flag("TEMP_CLEANUP_FOREIGN", f"temporal de {o.name!r} sustituido por objeto ajeno (preservado)", o.name)
        elif mr.status == _QUARANTINE_FAILED:
            ctx.flag("TEMP_CLEANUP_FAILED", f"no se pudo poner en cuarentena el temporal de {o.name!r}", o.name)

    def _rollback_one(o: _Out) -> None:  # B111: contención total por output
        try:
            if not o.promoted:
                return
            if not o.existed_before:  # ausente: deshaz nuestra creación por cuarentena (B125)
                mr = quar.move(o, o.name, o.temp_fd, phase=_ROLLING_BACK, reason="new-output-undo")
                if mr.status == _FOREIGN_OBJECT_PRESERVED:  # movimos una actualización concurrente → devuélvela
                    if quar.restore(o, mr, o.dir_fd, o.name):
                        ctx.flag("ABSENT_CONCURRENT_RETURNED", f"{o.name!r}: actualización concurrente devuelta a la ruta oficial", o.name)  # fmt: skip
                    else:
                        ctx.flag("ABSENT_CONCURRENT_ORPHANED", f"{o.name!r}: concurrente en cuarentena, ruta oficial ocupada", o.name)  # fmt: skip
                    o.concurrent_update = True
                elif mr.status == _QUARANTINE_FAILED:
                    ctx.flag("ABSENT_UNDO_FAILED", f"no se pudo revertir {o.name!r} (cuarentena falló)", o.name)
                else:
                    o.restored = True
                return
            _cas_restore(o, ctx)  # preexistente promovido → restaura el original por CAS
        except Exception as exc:  # noqa: BLE001 — contención total (B111)
            ctx.flag("ROLLBACK_ABORTED", f"rollback de {o.name!r} abortó ({type(exc).__name__}: {exc})", o.name)
            o.incomplete = True

    def _rollback() -> None:  # B106/B111/B112: NO eleva; contiene cada output; consume CADA resultado
        ctx.phase = _ROLLING_BACK
        for o in reversed(outs):
            _rollback_one(o)
        for o in outs:
            _quarantine_temp(o)
        for o in outs:  # R9.2R11 §2 regla 6: un exchange aplicado y no compensado ⇒ INCOMPLETO
            if o.exchange_applied and not o.compensation_verified:
                ctx.flag("EXCHANGE_UNCOMPENSATED", f"{o.name!r}: exchange aplicado sin compensación verificada", o.name)
                o.incomplete = True
        _abort_receipt(chain.camp, quar.txid, ctx)  # B144/B150: recibo huérfano → dir aborted gobernado (fail-closed)
        if not _fsync_dirs():  # B130: un fsync fallido en el rollback ⇒ INCOMPLETO, no sólo texto
            ctx.flag("ROLLBACK_FSYNC_FAILED", "fsync de directorios falló durante el rollback")

    try:
        ctx.phase = _PREPARING
        for i, o in enumerate(outs):  # 1. temporales
            data = o.df.to_csv(index=False).encode()
            o.temp_name, o.temp_fd = _create_governed(o.dir_fd, o.name, "tmp", i)
            o.temp_created = True
            with os.fdopen(o.temp_fd, "wb", closefd=False) as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            o.temp_digest = digest_fd(o.temp_fd)
        for o in outs:  # 2. lease del previo (B114) + bytes de confianza; SIN backup escrito (el exchange desplaza)
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
        _revalidate_leases("después de cargar")
        for o in outs:  # 3. binding + digest del temporal ANTES de promover
            assert o.temp_name is not None
            prob = _binding_problem(o.dir_fd, o.temp_name, o.temp_fd, mode=0o600)
            if prob is not None:
                raise _ValidationError(f"temporal de {o.name!r} comprometido antes de promover: {prob}")
            if digest_fd(o.temp_fd) != o.temp_digest:
                raise _ValidationError(f"temporal de {o.name!r} mutado antes de promover (digest)")
        for o in outs:  # 3b. B114: el target no cambió desde el snapshot
            if o.existed_before:
                assert o.orig_snapshot is not None and o.orig_digest is not None
                lp = lease_problem(o.dir_fd, o.name, o.orig_fd, o.orig_snapshot, o.orig_digest)
                if lp is not None:
                    raise _ValidationError(f"output {o.name!r} modificado por un tercero desde el snapshot: {lp}")
            elif _inode(o.dir_fd, o.name) is not None:
                raise _ValidationError(f"output {o.name!r} ausente al inicio fue CREADO por un tercero")
        ctx.phase = _PROMOTING
        for o in outs:  # 4. promoción por CAS atómico con estado explícito
            _cas_promote(o, ctx)
        for o in outs:  # 5. verifica target↔temp_fd + digest
            prob = _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600)
            if prob is not None:
                raise _ValidationError(f"output {o.name!r} tras promover no liga al temporal creado: {prob}")
            if digest_fd(o.temp_fd) != o.temp_digest:
                raise _ValidationError(f"output {o.name!r} tras promover con digest distinto")
        if not _fsync_dirs():
            raise _ValidationError("fsync de directorios falló antes del commit")
        ctx.phase = _CERTIFYING
        _certify_commit(chain, lock, inputs, outs, quar, ctx)  # B131/B132: recibo + commit_reached (único sitio)
        ctx.phase = _CLEANING
        for o in outs:  # limpia los ORIGINALES desplazados (viven en temp_name) por cuarentena
            if not (o.existed_before and o.temp_name is not None):
                continue
            if _inode(o.dir_fd, o.temp_name) is None:
                continue
            mr = quar.move(o, o.temp_name, o.orig_fd, phase=_CLEANING, reason="post-commit-original-cleanup")
            if mr.status == _FOREIGN_OBJECT_PRESERVED:
                ctx.flag("POSTCOMMIT_FOREIGN", f"original desplazado de {o.name!r} sustituido por objeto ajeno (preservado)", o.name)  # fmt: skip
            elif mr.status == _QUARANTINE_FAILED:
                ctx.flag("POSTCOMMIT_CLEANUP_FAILED", f"no se pudo poner en cuarentena el original desplazado de {o.name!r}", o.name)  # fmt: skip
        if not _fsync_dirs():
            ctx.flag("POSTCOMMIT_FSYNC_FAILED", "fsync de durabilidad post-commit falló")
        _publish_bundle(chain, quar, inputs, outs, campaign, ctx)  # B148/B145: sella la AUTORIDAD durable + CURRENT
    except Exception as primary:  # noqa: BLE001 — dominio/OSError sí; KeyboardInterrupt/SystemExit NO
        ctx.primary_error = primary
        if not ctx.commit_reached:
            _rollback()  # B106/B111: NO eleva


def _raise_outcome(ctx: _TxContext) -> None:
    """Clasifica por INVARIANTES (B127/B130/B139). Cualquier Issue 'incomplete' o cierre fallido cambia la
    clasificación; nunca queda como texto inerte."""
    incomplete = ctx.incomplete_issues()
    close = ctx.close_errors
    if ctx.commit_reached:
        problems: list[str] = []
        if ctx.primary_error is not None:
            problems.append(f"excepción post-commit: {ctx.primary_error!r}")
        problems += [repr(i) for i in incomplete]
        problems += [f"cierre: {e}" for e in close]
        if problems:
            raise CommittedStateError(
                f"COMMIT CRUZADO (outputs nuevos son la AUTORIDAD y son durables) pero quedó estado incompleto: "
                f"{problems}. NO reintentar como si hubiera rollback."
            )
        return  # éxito certificado
    if ctx.primary_error is None and not incomplete and not close:
        return  # nada que reportar (no hubo error)
    detail = f"{ctx.primary_error!r}; recuperaciones: {ctx.recoveries}; issues: {[repr(i) for i in ctx.issues]}; cierres: {close}"  # fmt: skip
    if incomplete or close:  # B127/B130/B139: divergencia externa / durabilidad → NO reintentar
        err_i = RollbackIncompleteError(f"ROLLBACK INCOMPLETO (no reintentar automáticamente): {detail}")
        if ctx.primary_error is not None:
            raise err_i from ctx.primary_error
        raise err_i
    if ctx.primary_error is None:
        raise RollbackError(f"issues sin error primario: {[repr(i) for i in ctx.issues]}")
    raise RollbackError(detail) from ctx.primary_error


def merge() -> int:
    campaign = os.environ.get("CAMPAIGN_ID")
    if campaign is not None and not campaign.strip():
        _fail("CAMPAIGN_ID definido pero vacío")
    chain = _Chain()
    lock: _LockGuard | None = None
    inputs: list[_InputLease] = []
    outs: list[_Out] = []
    quar = _Quarantine(f"{os.getpid()}.{secrets.token_hex(8)}")
    ctx = _TxContext()
    try:
        lock = _acquire_lock(chain.camp)
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
        _promote_transactionally(chain, lock, inputs, outs, quar, ctx, campaign)
    finally:
        ctx.phase = _CLOSED
        for o in outs:
            o.close_fds(ctx.close_errors)
        for lease in inputs:
            lease.close(ctx.close_errors)
        quar.close(ctx.close_errors)
        if ctx.receipt_fd >= 0:  # B149: cierre único del fd del recibo (se reportan fallos, no se tragan)
            try:
                os.close(ctx.receipt_fd)
            except OSError as exc:
                ctx.close_errors.append(f"cerrar recibo: {exc}")
            ctx.receipt_fd = -1
        if lock is not None and lock.fd >= 0:
            try:
                os.close(lock.fd)
            except OSError as exc:
                ctx.close_errors.append(f"cerrar lock: {exc}")
        chain.close(ctx.close_errors)
    _raise_outcome(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(merge())
