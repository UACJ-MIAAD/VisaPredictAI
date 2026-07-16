#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74/B79..B109/B110-B118 — extraído del heredoc de
run_campaign_aq{,_tail}.sh). Se invoca desde ROOT (run-command fija cwd=root).

Máquina de estados EXPLÍCITA (`_TxContext.phase`, R9.2R9): LOADING → PREPARING → PROMOTING →
{ROLLING_BACK | COMMIT_REACHED} → CLEANING → CLOSED. La clasificación del resultado depende del estado:
un fallo ANTES del commit ⇒ `RollbackError`; DESPUÉS ⇒ `CommittedStateError` (outputs nuevos = autoridad
durable). `_promote_transactionally()` NUNCA deja escapar un error operativo; `primary_error` JAMÁS se
ignora (B110: una excepción post-commit tipa `CommittedStateError`, no un verde). El rollback contiene CADA
output por separado y prosigue con los ocho (B111); consume y registra CADA resultado de limpieza (B112). La
recuperación es una subtransacción total que jamás eleva (B118). Los errores de cierre de la cadena, leases,
lock y artefactos se REPORTAN (B113) y se ADJUNTAN al error de la fase, nunca lo reemplazan (B109).

Validación en dominio, no por proceso (R9.2R9): dentro de la transacción una violación es `_ValidationError`
(atrapada → rollback); `KeyboardInterrupt`/`SystemExit` NO se atrapan como fallos ordinarios y propagan.

LEASES vivos hasta el commit:
- Entradas (B115): las OCHO mitades se abren gobernadas y su fd queda ABIERTO; el DataFrame se parsea de los
  BYTES leídos de ESE descriptor (digest incluido) y el lease se revalida (nombre↔inode, snapshot fstat,
  digest) tras cargar las ocho, antes de promover e inmediatamente antes del commit. Un input cambiado tras
  leerse aborta: el output ya no correspondería al CSV oficial.
- Salidas preexistentes (B114): además de la copia de confianza `previous_bytes`, se retiene un fd + snapshot
  + digest del inode ORIGINAL. Antes de promover se confirma que el target sigue ligado a ese lease (un output
  ausente sigue ausente); si un tercero lo modificó/creó, se ABORTA sin sobrescribir. El rollback nunca
  restaura bytes viejos sobre una actualización concurrente (verifica que el target aún liga a NUESTRO
  temporal antes de restaurar).
- Lock (B116): `.merge.lock` es un LEASE (`_LockGuard` con dev/ino/uid/modo). Tras el flock se revalida que el
  NOMBRE sigue ligado al MISMO inode del fd bloqueado (un unlink+recreate del lock tras el flock aborta) en
  cada checkpoint pre-commit.

Limpieza por CUARENTENA, no unlink destructivo (B117): POSIX no ofrece un unlink 'por fd'. Temporales y
respaldos NO se borran con binding-check→unlink (carrera TOCTOU); se MUEVEN por `rename` fd-relativo a
`.merge-quarantine/<transaction_id>/` y se verifica que el objeto en cuarentena liga al fd esperado, con un
manifiesto 0600 (nombre, digest, inode, fase, motivo). Un objeto ajeno se PRESERVA (nunca se destruye) y
produce error tipado. La recolección de la cuarentena queda para el finalizador P2b (esta ronda no hace GC).

GOBERNANZA DE RUTAS (B90): la cadena `.` → `reports` → `campaign`/`eval` se abre COMPONENTE A COMPONENTE con
`openat` `O_DIRECTORY|O_NOFOLLOW`; cada nivel exige directorio real, del UID actual y sin escritura de grupo/
otros. Los descriptores quedan ABIERTOS toda la transacción y CADA operación es fd-relativa. La identidad
(st_dev/st_ino) se REVERIFICA tras adquirir el lock, antes/después de promover y en el punto de commit.

FAIL-CLOSED sobre el esquema REAL (B79/B80/B85): OCHO mitades con las 19 columnas canónicas en orden, un ÚNICO
`run_id` string no vacío, `table` coincidente, strings no vacíos y métricas donde el NaN REAL (celda vacía) se
distingue del texto coercionado (bloqueado) y del infinito (bloqueado); `secs` ≥ 0. Identidad de campaña
(B85): con `CAMPAIGN_ID` en el entorno las ocho mitades deben llevarla; standalone conserva el máximo
lexicográfico + `source_run_id`.

Garantías de escritura (honestas): validación GLOBAL previa; promoción ATÓMICA por fichero; rollback
transaccional DURABLE (restaura byte-idéntico o recupera desde bytes de confianza; fsync de ambos directorios
también en el error). NO es atomicidad de bundle crash-safe (un kill a mitad puede dejar estado parcial); esa
garantía, con manifiesto final, vive en P2b antes de F2.
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
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Fases explícitas de la transacción (R9.2R9 §2).
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
    """R9.2R9 §2: violación de invariante DENTRO de la transacción — es un fallo de dominio ORDINARIO
    (atrapado → rollback/clasificación), a diferencia de `KeyboardInterrupt`/`SystemExit` que propagan."""


class RollbackError(OSError):
    """B104: fallo ANTES del punto de commit — la transacción se REVIRTIÓ (outputs previos restaurados o
    recuperación verificable materializada). Reintentar es seguro."""


class CommittedStateError(RuntimeError):
    """B104/B110: el punto de commit SÍ se cruzó — los outputs nuevos son la AUTORIDAD y son durables — pero
    quedó estado incompleto (limpieza/fsync/cierre fallido O una excepción posterior). NUNCA confundir con un
    rollback: reintentar a ciegas es incorrecto."""


def _open_dir_at(parent_fd: int | None, name: str, label: str) -> int:
    """B90: un componente de la cadena gobernada. O_DIRECTORY|O_NOFOLLOW ⇒ un symlink revienta en el open; el
    fstat del DESCRIPTOR exige dir real, del UID actual y sin escritura de grupo/otros."""
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
        """B113: un fallo cerrando cualquiera de los 4 descriptores de la cadena se REPORTA (antes se tragaba)."""
        for label, fd in zip(("dot", "reports", "campaign", "eval"), self.fds(), strict=True):
            try:
                os.close(fd)
            except OSError as exc:
                if errs is not None:
                    errs.append(f"cerrar cadena {label}: {exc}")

    def reverify(self, when: str) -> None:
        """R9.2R9: una discrepancia de identidad es `_ValidationError` (dominio) — atrapada por la transacción
        y clasificada; el llamador pre-transacción (merge, tras el lock) la convierte en `_fail`/SystemExit."""
        fresh = _Chain()
        try:
            if fresh.idents() != self.idents():
                raise _ValidationError(f"la cadena reports/campaign|eval cambió de identidad ({when}) — swap")
        finally:
            fresh.close()


class _LockGuard:
    """B116: lock gobernado como LEASE. Captura fd + (dev,ino,uid) del inode bloqueado tras el flock; `problem`
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
    """B102/B103: el NOMBRE dentro de dir_fd debe apuntar al MISMO inode (dev/ino) que `fd` — y `fd` regular/
    UID/nlink==1 (+ modo si se exige). Un mismatch = el nombre fue sustituido: jamás autoriza operar por él."""
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


def _create_governed(dir_fd: int, base: str, kind: str, i: int) -> tuple[str, int]:
    """B100: crea un artefacto con nombre de NONCE aleatorio + PID + índice vía `O_CREAT|O_EXCL|O_NOFOLLOW`.
    Devuelve (name, fd r/w VIVO)."""
    name = f".{base}.{kind}.{os.getpid()}.{i}.{secrets.token_hex(8)}"
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    return name, fd


# --------------------------------------------------------------------------------------------------------
# Cuarentena (B117): mover objetos a `.merge-quarantine/<txid>/` en vez de unlink destructivo.
# --------------------------------------------------------------------------------------------------------

# Resultado EXPLÍCITO de una cuarentena/limpieza (B105/B112): nunca un retorno silencioso.
_QUARANTINED = "QUARANTINED"  # nuestro objeto salió del árbol vivo hacia la cuarentena inventariada
_ALREADY_ABSENT = "ALREADY_ABSENT"
_FOREIGN_OBJECT_PRESERVED = "FOREIGN_OBJECT_PRESERVED"  # se movió/preservó un objeto ajeno (no se destruyó)
_QUARANTINE_FAILED = "QUARANTINE_FAILED"


class _Quarantine:
    """B117: gestor de `.merge-quarantine/<txid>/` por descriptor de directorio gobernado. Crea el árbol
    fd-relativo (tolera EEXIST del contenedor, exige dir real/propio/no-escribible), mueve objetos con `rename`
    fd-relativo, verifica el binding tras el move y escribe un manifiesto 0600. NUNCA borra: preserva."""

    __slots__ = ("txid", "_qdirs", "_manifests", "fds")

    def __init__(self, txid: str) -> None:
        self.txid = txid
        self._qdirs: dict[int, int] = {}  # dir_fd -> fd del subdir <txid>
        self._manifests: dict[int, int] = {}  # dir_fd -> fd del manifiesto (append)
        self.fds: list[int] = []

    def _qdir_for(self, dir_fd: int) -> int:
        if dir_fd in self._qdirs:
            return self._qdirs[dir_fd]
        try:
            os.mkdir(_QUARANTINE_DIR, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
        qroot = os.open(_QUARANTINE_DIR, _DIR_FLAGS, dir_fd=dir_fd)
        self.fds.append(qroot)
        st = os.fstat(qroot)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or (stat.S_IMODE(st.st_mode) & 0o022):
            raise _ValidationError(f"{_QUARANTINE_DIR} ajeno o escribible por grupo/otros")
        os.mkdir(self.txid, 0o700, dir_fd=qroot)  # nonce → no debería existir; EEXIST reventaría (bien)
        qtx = os.open(self.txid, _DIR_FLAGS, dir_fd=qroot)
        self.fds.append(qtx)
        self._qdirs[dir_fd] = qtx
        return qtx

    def _manifest_for(self, dir_fd: int, qtx: int) -> int:
        if dir_fd in self._manifests:
            return self._manifests[dir_fd]
        mfd = os.open("MANIFEST.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=qtx)
        self.fds.append(mfd)
        self._manifests[dir_fd] = mfd
        return mfd

    def move(self, o: _Out, name: str | None, fd: int, *, phase: str, reason: str) -> str:
        """Mueve `name` a la cuarentena y verifica el binding del objeto movido. QUARANTINED si liga a NUESTRO
        `fd`; FOREIGN_OBJECT_PRESERVED si movimos/preservamos un objeto ajeno; ALREADY_ABSENT si no había nada;
        QUARANTINE_FAILED ante un error de syscall. Escribe un manifiesto 0600 con nombre/digest/inode/fase/
        motivo. Nunca eleva por un fallo operativo (lo devuelve como estado)."""
        if name is None or fd < 0:
            return _ALREADY_ABSENT
        try:
            os.stat(name, dir_fd=o.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _ALREADY_ABSENT
        except OSError:
            return _QUARANTINE_FAILED
        digest = None
        try:
            digest = digest_fd(fd)  # digest de NUESTRO inode (para el manifiesto), antes de mover
        except OSError:
            pass
        try:
            qtx = self._qdir_for(o.dir_fd)
        except OSError, _ValidationError:
            return _QUARANTINE_FAILED
        qname = f"{o.label}.{name.lstrip('.')}.{secrets.token_hex(6)}"
        try:
            os.rename(name, qname, src_dir_fd=o.dir_fd, dst_dir_fd=qtx)
        except FileNotFoundError:
            return _ALREADY_ABSENT
        except OSError:
            return _QUARANTINE_FAILED
        bound = _binding_problem(qtx, qname, fd, mode=0o600) is None
        try:
            st = os.stat(qname, dir_fd=qtx, follow_symlinks=False)
            inode = [st.st_dev, st.st_ino]
        except OSError:
            inode = None
        self._write_manifest(o, qtx, name, qname, digest, inode, phase, reason, bound)
        return _QUARANTINED if bound else _FOREIGN_OBJECT_PRESERVED

    def _write_manifest(
        self,
        o: _Out,
        qtx: int,
        name: str,
        qname: str,
        digest: str | None,
        inode: list[int] | None,
        phase: str,
        reason: str,
        bound: bool,
    ) -> None:
        rec = {
            "label": o.label, "orig_name": name, "quarantined_as": qname, "digest": digest,
            "inode": inode, "phase": phase, "reason": reason, "bound_to_tx_fd": bound,
        }  # fmt: skip
        try:
            mfd = self._manifest_for(o.dir_fd, qtx)
            os.write(mfd, (json.dumps(rec, sort_keys=True) + "\n").encode())
        except OSError:
            pass  # el objeto ya está preservado en cuarentena; un manifiesto ilegible no lo desprotege

    def close(self, errs: list[str]) -> None:
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError as exc:
                errs.append(f"cerrar fd de cuarentena: {exc}")
        self.fds.clear()


class _Out:
    """Estado transaccional por output con PROPIEDAD por descriptor (B100–B103) y LEASE del inode previo
    (B114). `*_created=True` solo tras un `O_EXCL` exitoso; `orig_fd` es el lease del output preexistente."""

    __slots__ = (
        "dir_fd", "label", "name", "df",
        "existed_before", "previous_bytes", "previous_digest",
        "orig_fd", "orig_snapshot", "orig_digest",
        "temp_created", "temp_name", "temp_fd", "temp_digest",
        "backup_created", "backup_name", "backup_fd", "backup_digest",
        "promoted", "recovered", "concurrent_update",
        "recovery_created", "recovery_name", "recovery_fd", "recovery_digest", "recovery_promoted",
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
        self.backup_created = False
        self.backup_name: str | None = None
        self.backup_fd = -1
        self.backup_digest: str | None = None
        self.promoted = False
        self.recovered = False
        self.concurrent_update = False
        self.recovery_created = False
        self.recovery_name: str | None = None
        self.recovery_fd = -1
        self.recovery_digest: str | None = None
        self.recovery_promoted = False

    def close_fds(self, errs: list[str]) -> None:
        # Cierra recovery → backup → temp → orig (lease del previo). Los errores se REPORTAN (B109/B113).
        for fd_attr in ("recovery_fd", "backup_fd", "temp_fd", "orig_fd"):
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
    """Resultado transaccional compartido (B104/B109/B110): CLASIFICA cualquier fallo por el estado (`phase`).
    Un error secundario (rollback/cleanup/close) NUNCA reemplaza `primary_error`; se ADJUNTA."""

    __slots__ = ("phase", "commit_reached", "primary_error", "rollback_errors", "recoveries", "postcommit_errors", "close_errors")  # fmt: skip

    def __init__(self) -> None:
        self.phase = _LOADING
        self.commit_reached = False
        self.primary_error: BaseException | None = None
        self.rollback_errors: list[str] = []
        self.recoveries: list[str] = []
        self.postcommit_errors: list[str] = []
        self.close_errors: list[str] = []


def _target_binds(o: _Out, fd: int) -> bool:
    """True si el target `o.name` dentro de `o.dir_fd` liga (dev/ino) al inode de `fd`. Guarda toda OSError."""
    try:
        return _binding_problem(o.dir_fd, o.name, fd, mode=None) is None
    except OSError:
        return False


def _recover_from_bytes(o: _Out, errs: list[str], recoveries: list[str]) -> None:
    """B98/B103/B106/B118: subtransacción de recuperación TOTAL desde `previous_bytes` (copia de confianza).
    JAMÁS deja escapar una excepción — un guard externo captura CUALQUIER Exception de cualquier syscall
    (creación/escritura/flush/fsync/digest/binding/promoción/verificación); cada rama registra su motivo en
    `errs` o la ruta confirmada en `recoveries`. El fd del recovery queda en `o.recovery_fd` para el cierre."""
    try:
        _recover_from_bytes_inner(o, errs, recoveries)
    except Exception as exc:  # noqa: BLE001 — B118: la recuperación nunca interrumpe el rollback global
        errs.append(f"recuperación de {o.name!r} abortó ({type(exc).__name__}: {exc})")


def _recover_from_bytes_inner(o: _Out, errs: list[str], recoveries: list[str]) -> None:
    if o.previous_bytes is None or o.previous_digest is None:
        errs.append(f"sin bytes previos de confianza para recuperar {o.name!r}")
        return
    try:
        o.recovery_name, o.recovery_fd = _create_governed(o.dir_fd, o.name, "rec", 0)
        o.recovery_created = True
    except OSError as exc:
        errs.append(f"crear recuperación de {o.name!r}: {exc}")
        return
    try:
        with os.fdopen(o.recovery_fd, "wb", closefd=False) as rf:
            rf.write(o.previous_bytes)
            rf.flush()
            os.fsync(rf.fileno())
        o.recovery_digest = digest_fd(o.recovery_fd)
        if o.recovery_digest != o.previous_digest:
            errs.append(f"recuperación de {o.name!r} con digest inconsistente")
            return
        if _binding_problem(o.dir_fd, o.recovery_name, o.recovery_fd, mode=0o600) is not None:
            errs.append(f"recuperación de {o.name!r}: nombre no liga al descriptor")
            return
    except OSError as exc:
        errs.append(f"escribir recuperación de {o.name!r}: {exc}")
        return
    # B114: nunca promover el recovery encima de una actualización concurrente (target ya no liga a NUESTRO
    # temporal). Si un tercero cambió el target tras promoverlo nosotros, se PRESERVA bajo el nombre aleatorio.
    concurrent = o.promoted and not _target_binds(o, o.temp_fd)
    if not concurrent:
        try:
            os.replace(o.recovery_name, o.name, src_dir_fd=o.dir_fd, dst_dir_fd=o.dir_fd)
            os.fsync(o.dir_fd)
            if (
                _binding_problem(o.dir_fd, o.name, o.recovery_fd, mode=0o600) is None
                and digest_fd(o.recovery_fd) == o.previous_digest
            ):
                o.recovered = True
                o.recovery_promoted = True
                recoveries.append(f"RECUPERACIÓN PRESERVADA reports/{o.label}/{o.name} (de {o.name!r})")
                return
            errs.append(f"recuperación promovida de {o.name!r} no verifica")
        except OSError:
            pass  # cae a preservar bajo el nombre aleatorio
    else:
        o.concurrent_update = True
        errs.append(f"{o.name!r} tiene una actualización concurrente; recuperación PRESERVADA aparte, no sobrescrita")
    try:
        os.fsync(o.dir_fd)
    except OSError as exc:
        errs.append(f"fsync tras recuperación de {o.name!r}: {exc}")
    try:
        preserved = (
            _binding_problem(o.dir_fd, o.recovery_name, o.recovery_fd, mode=0o600) is None
            and digest_fd(o.recovery_fd) == o.previous_digest
        )
    except OSError as exc:
        errs.append(f"verificar recuperación de {o.name!r}: {exc}")
        return
    if preserved:
        recoveries.append(f"RECUPERACIÓN PRESERVADA reports/{o.label}/{o.recovery_name} (de {o.name!r})")
    else:
        errs.append(f"recuperación de {o.name!r} no verificable en disco")


def _promote_transactionally(
    chain: _Chain, lock: _LockGuard, inputs: list[_InputLease], outs: list[_Out], quar: _Quarantine, ctx: _TxContext
) -> None:
    """Transacción con propiedad por descriptor, leases y estado clasificable (B90..B118). NUNCA deja escapar
    un error operativo: todo se recopila en `ctx` y se clasifica en `_raise_outcome`."""

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

    def _rollback_one(o: _Out) -> None:
        """B111: revierte UN output. Toda excepción se captura y registra; jamás interrumpe a los demás."""
        errs, recoveries = ctx.rollback_errors, ctx.recoveries
        try:
            if not o.promoted:
                return
            if not o.existed_before:  # ausente antes → cuarentena del nuevo SOLO si liga a NUESTRO temporal
                res = quar.move(o, o.name, o.temp_fd, phase=_ROLLING_BACK, reason="new-output-undo")
                if res == _FOREIGN_OBJECT_PRESERVED:
                    errs.append(f"{o.name!r} tras promover no liga al temporal; objeto ajeno PRESERVADO")
                elif res == _QUARANTINE_FAILED:
                    errs.append(f"no se pudo revertir {o.name!r} (ausente antes)")
                return
            if not _target_binds(o, o.temp_fd):  # B114: un tercero cambió el target tras promover → NO clobber
                o.concurrent_update = True
                errs.append(f"{o.name!r} con actualización concurrente tras promover; NO se restauran bytes viejos")
                return
            restored = False  # existía antes → restaura del backup SOLO si liga a NUESTRO fd Y digest coincide
            if o.backup_created and o.backup_name is not None:
                try:
                    bprob = _binding_problem(o.dir_fd, o.backup_name, o.backup_fd, mode=0o600)
                    bytes_ok = bprob is None and digest_fd(o.backup_fd) == o.previous_digest
                except OSError as exc:
                    bprob, bytes_ok = f"error ({exc})", False
                    errs.append(f"binding/digest del backup de {o.name!r}: {exc}")
                if bytes_ok:
                    try:
                        os.replace(o.backup_name, o.name, src_dir_fd=o.dir_fd, dst_dir_fd=o.dir_fd)
                        vfd = os.open(o.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=o.dir_fd)
                        try:
                            restored = digest_fd(vfd) == o.previous_digest
                        finally:
                            os.close(vfd)
                        if restored:
                            o.recovered = True
                        else:
                            errs.append(f"restauración de {o.name!r} no coincide con el digest original")
                    except OSError as exc:
                        errs.append(f"restaurar {o.name!r} desde backup: {exc}")
                else:
                    errs.append(f"backup de {o.name!r} no fiable ({bprob or 'digest distinto'}); se recupera de bytes")
            if not restored:  # backup ausente/sustituido/inconsistente → recupera de bytes de confianza
                _recover_from_bytes(o, errs, recoveries)
                if not o.recovered and not o.concurrent_update:
                    errs.append(f"NO se pudo recuperar {o.name!r}")
        except Exception as exc:  # noqa: BLE001 — B111: contención total por output
            errs.append(f"rollback de {o.name!r} abortó ({type(exc).__name__}: {exc})")

    def _rollback() -> None:  # B106/B111/B112: NO eleva; contiene cada output; consume CADA resultado de limpieza
        ctx.phase = _ROLLING_BACK
        errs = ctx.rollback_errors
        for o in reversed(outs):  # deshace promociones en orden INVERSO
            _rollback_one(o)
        for o in outs:  # cuarentena de temporales SOLO si ligan a NUESTRO fd (B112: registra el resultado)
            res = quar.move(o, o.temp_name, o.temp_fd, phase=_ROLLING_BACK, reason="temp-cleanup")
            if res == _FOREIGN_OBJECT_PRESERVED:
                errs.append(f"temporal de {o.name!r} sustituido por objeto ajeno (preservado en cuarentena)")
            elif res == _QUARANTINE_FAILED:
                errs.append(f"no se pudo poner en cuarentena el temporal de {o.name!r}")
        for o in outs:  # backups: cuarentena EXCEPTO los cuya restauración/recuperación no se confirmó (B98)
            if o.promoted and o.existed_before and not o.recovered:
                continue  # se conserva bajo su nombre como última copia recuperable
            res = quar.move(o, o.backup_name, o.backup_fd, phase=_ROLLING_BACK, reason="backup-cleanup")
            if res == _FOREIGN_OBJECT_PRESERVED:
                errs.append(f"backup de {o.name!r} sustituido por objeto ajeno (preservado en cuarentena)")
            elif res == _QUARANTINE_FAILED:
                errs.append(f"no se pudo poner en cuarentena el backup de {o.name!r}")
        try:
            _fsync_dirs()  # B92
        except OSError as exc:
            errs.append(f"fsync de directorios: {exc}")

    try:
        ctx.phase = _PREPARING
        for i, o in enumerate(outs):  # 1. temporales: O_EXCL → registra fd → escribe → fsync → digest
            data = o.df.to_csv(index=False).encode()
            o.temp_name, o.temp_fd = _create_governed(o.dir_fd, o.name, "tmp", i)
            o.temp_created = True
            with os.fdopen(o.temp_fd, "wb", closefd=False) as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            o.temp_digest = digest_fd(o.temp_fd)
        for i, o in enumerate(outs):  # 2. lease del previo (B114) + snapshot de confianza (B108) → backup O_EXCL
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
            o.backup_name, o.backup_fd = _create_governed(o.dir_fd, o.name, "bak", i)
            o.backup_created = True
            with os.fdopen(o.backup_fd, "wb", closefd=False) as bfh:
                bfh.write(prev)
                bfh.flush()
                os.fsync(bfh.fileno())
            o.backup_digest = digest_fd(o.backup_fd)
            if o.backup_digest != o.previous_digest:
                raise _ValidationError(f"backup de {o.name!r} no coincide con el output previo (digest)")
        _revalidate_leases("después de cargar y respaldar")  # B115/B116/B90 (checkpoint tras cargar las ocho)
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
                    raise _ValidationError(
                        f"output {o.name!r} ausente al inicio fue CREADO por un tercero; no se sobrescribe"
                    )
                except FileNotFoundError:
                    pass
        ctx.phase = _PROMOTING
        for o in outs:  # 4. promueve (atómico por fichero, fd-relativo)
            assert o.temp_name is not None
            os.replace(o.temp_name, o.name, src_dir_fd=o.dir_fd, dst_dir_fd=o.dir_fd)
            o.promoted = True
        for o in outs:  # 5. verifica que el target liga al temporal que creamos + digest (B102)
            prob = _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600)
            if prob is not None:
                raise _ValidationError(f"output {o.name!r} tras promover no liga al temporal creado: {prob}")
            if digest_fd(o.temp_fd) != o.temp_digest:
                raise _ValidationError(f"output {o.name!r} tras promover con digest distinto")
        _fsync_dirs()
        _revalidate_leases("punto de commit")  # B99/B115/B116: cadena+leases+lock con los backups AÚN presentes
        for o in outs:  # 6. B107: re-verifica los 8 target (binding + digest + modo/UID/nlink) JUSTO antes del commit
            prob = _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600)
            if prob is not None:
                raise _ValidationError(f"output {o.name!r} alterado antes del commit: {prob}")
            if digest_fd(o.temp_fd) != o.temp_digest:
                raise _ValidationError(f"output {o.name!r} mutado antes del commit (digest) — contenido falsificado")
        ctx.commit_reached = True  # ---- PUNTO DE COMMIT ----
        ctx.phase = _COMMIT_REACHED
        ctx.phase = _CLEANING
        for o in outs:  # limpia backups por CUARENTENA con resultado EXPLÍCITO (B105/B112/B117)
            res = quar.move(o, o.backup_name, o.backup_fd, phase=_CLEANING, reason="post-commit-backup-cleanup")
            if res == _FOREIGN_OBJECT_PRESERVED:
                ctx.postcommit_errors.append(f"backup de {o.name!r} sustituido por objeto ajeno (preservado)")
            elif res == _QUARANTINE_FAILED:
                ctx.postcommit_errors.append(f"no se pudo poner en cuarentena el backup de {o.name!r}")
        try:
            _fsync_dirs()
        except OSError as exc:
            ctx.postcommit_errors.append(f"fsync de durabilidad post-commit: {exc}")
    except Exception as primary:  # noqa: BLE001 — R9.2R9 §2: dominio/OSError sí; KeyboardInterrupt/SystemExit NO
        ctx.primary_error = primary
        if not ctx.commit_reached:
            _rollback()  # B106/B111: NO eleva


def _raise_outcome(ctx: _TxContext) -> None:
    """Clasifica el resultado por el estado. Post-commit ⇒ CommittedStateError si HAY cualquier error primario,
    de cleanup o de cierre (B110: `primary_error` post-commit JAMÁS se ignora). Pre-commit ⇒ RollbackError con
    recuperaciones/errores de rollback/cierres. Éxito ⇒ retorna. Un error de cierre nunca reemplaza el primario."""
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
    if ctx.primary_error is None:
        if ctx.rollback_errors or ctx.close_errors:
            raise RollbackError(
                f"errores sin error primario: rollback={ctx.rollback_errors}; cierre={ctx.close_errors}"
            )
        return
    detail = (
        f"{ctx.primary_error!r}; recuperaciones: {ctx.recoveries}; errores de rollback: {ctx.rollback_errors}; "
        f"cierres: {ctx.close_errors}"
    )
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
        # 1. Carga + valida las OCHO mitades como LEASES ANTES de escribir nada (B115), bajo el lock, fd-bound.
        for table in _TABLES:
            for block in _BLOCKS:
                halves = [
                    _lease_half(chain.camp, f"aq_pool_{kind}_{table}_{block}.csv", table, campaign) for kind in _HALVES
                ]
                inputs.extend(halves)
                full = pd.concat([h.df for h in halves], ignore_index=True)
                full["source_run_id"] = full["run_id"]
                # B85: bajo campaña el run_id ES la campaña; standalone = máximo LEXICOGRÁFICO (string).
                full["run_id"] = campaign if campaign is not None else str(full["run_id"].astype(str).max())
                tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
                outs.append(_Out(chain.camp, "campaign", f"campaign_pool_{table}_{block}.csv", full))
                outs.append(_Out(chain.ev, "eval", tgt, full))
                print(f"{table}/{block}: {len(full)} rows -> {tgt}")
        # 2. Promoción transaccional fd-relativa; `ctx` clasifica el resultado (nunca eleva por sí sola).
        _promote_transactionally(chain, lock, inputs, outs, quar, ctx)
    finally:  # cierre único de los descriptores; errores → `ctx.close_errors` (B109/B113)
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
    _raise_outcome(ctx)  # B104/B109/B110: clasifica pre-commit (RollbackError) vs post-commit (CommittedStateError)
    return 0


if __name__ == "__main__":
    sys.exit(merge())
