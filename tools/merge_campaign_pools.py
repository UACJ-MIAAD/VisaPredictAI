#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74/B79/B80/B85/B89/B90/B92/B94/B95/B97/B98/B99 — extraído del heredoc
de run_campaign_aq{,_tail}.sh). Se invoca desde ROOT (run-command fija cwd=root).

Lectura de mitades gobernada (B95): cada CSV se lee por `governed_read.read_governed_csv` (nombre RELATIVO
validado — B96) — snapshot `fstat` pre/post exacto (regular/UID/nlink==1/**sin escritura de grupo-otros**/
dev·ino·size·mtime·ctime estables) del MISMO descriptor.

Transacción con estado EXPLÍCITO por output (`_Out`): temporales y backups se REGISTRAN en cuanto se crea su
fd, ANTES de escribir (un fallo de escritura ya es conocido por el rollback — B97); la reverificación FINAL de
la cadena ocurre con los backups aún presentes, definiendo el PUNTO DE COMMIT (B99: un swap final se detecta
mientras todavía se puede revertir); el rollback PRESERVA el backup de toda restauración fallida (B98: no se
destruye la última copia recuperable) y adjunta al error {target, operación, backup preservado, ruta de
recuperación}. Limpieza ESTRICTA (B94): un residuo tras el commit NO es éxito.

GOBERNANZA DE RUTAS (B90): la cadena `.` → `reports` → `campaign`/`eval` se abre COMPONENTE A COMPONENTE con
`openat` `O_DIRECTORY|O_NOFOLLOW` (ningún ancestro puede ser symlink) y cada nivel exige directorio real, del
UID actual y sin escritura de grupo/otros. Los descriptores quedan ABIERTOS toda la transacción y TODA
operación posterior (lock, lectura de mitades, temporales, respaldos, promoción, rollback, limpieza, fsync) es
fd-relativa — nada se re-resuelve por ruta tras validar. La identidad de la cadena (st_dev/st_ino) se
REVERIFICA tras adquirir el lock, antes de promover, después de promover y antes de devolver éxito: un swap de
ancestro aborta con rollback relativo a los descriptores ORIGINALES y el árbol externo queda intacto. Cada CSV
se abre con `openat O_NOFOLLOW`, se valida por `fstat` (regular/UID/nlink==1) y se entrega a pandas como file
object del MISMO descriptor (prohibido check-then-reopen).

FAIL-CLOSED sobre el esquema REAL de producción (B79/B80/B85): exige las OCHO mitades exactas (2 tablas × 2
bloques × 2 mitades) con EXACTAMENTE las 19 columnas canónicas en orden, un ÚNICO `run_id` no vacío tratado
como STRING (los reales son `20260706T114535-<sha>`), `table` coincidente con el nombre del fichero,
`model`/`country`/`category` strings no vacíos (isna ANTES de astype: NaN→"nan" enmascara el vacío), y métricas
donde se distingue el NaN REAL (celda vacía = modelo fallido, permitido) del TEXTO no numérico coercionado a
NaN (bloqueado) y del infinito (bloqueado); `secs` numérico ≥ 0. **Identidad de campaña (B85):** si
`CAMPAIGN_ID` está en el entorno (el runbook la exporta y `vp_model.config` la pinea como run_id), las OCHO
mitades deben llevar EXACTAMENTE ese run_id; standalone conserva el MÁXIMO LEXICOGRÁFICO + `source_run_id`.

Exclusión concurrente (B89): lock gobernado `.merge.lock` DENTRO del descriptor de campaign (0600, O_NOFOLLOW,
regular, del UID, nlink==1; el fstat se REPITE tras adquirir el `flock LOCK_EX` — un hardlink/chmod durante la
espera muere) sostenido durante carga+validación+respaldo+promoción+rollback+fsync.

Propiedad por DESCRIPTOR, no por nombre (B100–B103): cada temporal/backup se crea con nombre de nonce
aleatorio + `O_CREAT|O_EXCL|O_NOFOLLOW`; solo tras un open EXITOSO se marca `*_created` y se registra
(name, fd VIVO, dev, ino, digest sha256). El fd queda ABIERTO hasta commit o rollback. Antes de promover y
tras promover se re-verifica el BINDING nombre↔fd (`os.stat(name, follow_symlinks=False)` debe ligar al mismo
`dev/ino` que `fstat(fd)`) y el DIGEST — una sustitución del inode del temporal antes de `os.replace` se caza
(no se publica contenido inyectado). El rollback restaura desde el backup SOLO si su nombre sigue ligado al
`backup_fd` original Y su digest coincide con el del output previo; si el nombre desapareció o fue sustituido,
NO se usa: se materializa una recuperación verificable desde `previous_bytes` (copia de confianza en memoria) y
solo se anuncia "RECUPERACIÓN PRESERVADA" tras reabrirla y verificar identidad+digest+fsync. Jamás se borra un
objeto cuyo nombre ya no liga al descriptor que la transacción creó (B100).

Errores TIPADOS (B104): `RollbackError` = fallo ANTES del commit (revertido, con recuperaciones); antes se
usaba `OSError` genérico. `CommittedStateError` = el punto de commit SÍ se cruzó (los outputs nuevos son la
autoridad y son durables) pero la limpieza/fsync posterior falló — reintentar a ciegas es INCORRECTO.

Garantías de escritura (honestas): validación GLOBAL previa; promoción ATÓMICA por fichero; ROLLBACK
transaccional DURABLE (restaura byte-idéntico o recupera desde bytes de confianza; fsync de AMBOS directorios
también en el error — B92). NO es atomicidad de bundle crash-safe (un kill a mitad puede dejar estado parcial);
esa garantía, con manifiesto final, vive en P2b antes de F2.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import secrets
import stat
import sys
from typing import NoReturn

import pandas as pd

from tools.governed_read import read_governed_csv

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
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _fail(msg: str) -> NoReturn:
    print(f"merge_campaign_pools: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _open_dir_at(parent_fd: int | None, name: str, label: str) -> int:
    """B90: un componente de la cadena gobernada. O_DIRECTORY|O_NOFOLLOW ⇒ un symlink (sano o roto) revienta
    en el open; el fstat del DESCRIPTOR exige dir real, del UID actual y sin escritura de grupo/otros."""
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
    `reverify()` re-camina la cadena FRESCA desde cwd y exige la MISMA identidad (st_dev, st_ino) por nivel —
    un swap de ancestro tras la validación aborta en vez de operar sobre el árbol equivocado."""

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

    def close(self) -> None:
        for fd in self.fds():
            try:
                os.close(fd)
            except OSError:
                pass

    def reverify(self, when: str) -> None:
        fresh = _Chain()
        try:
            if fresh.idents() != self.idents():
                _fail(f"la cadena reports/campaign|eval cambió de identidad ({when}) — swap de ancestro")
        finally:
            fresh.close()


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


def _acquire_lock(camp_fd: int) -> int:
    """B89/B90: lock RELATIVO al descriptor de campaign (jamás por ruta). fstat antes Y después del flock —
    un hardlink/chmod plantado mientras esperábamos el lock también muere."""
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
    return fd


def _read_csv_at(dir_fd: int, fname: str) -> pd.DataFrame:
    """B90/B95: abre el CSV con openat O_NOFOLLOW y lo lee vía `read_governed_csv` — snapshot fstat pre/post
    exacto (regular/UID/nlink==1/no escribible por grupo-otros/dev·ino·size·mtime·ctime estables); pandas lee
    del MISMO descriptor. Un fichero escribible por terceros o mutado durante la lectura aborta."""
    df, err = read_governed_csv(dir_fd, fname, dtype={"run_id": str})
    if err is not None:
        _fail(f"mitad {fname!r}: {err}")
    assert df is not None
    return df


def _load_half(camp_fd: int, fname: str, table: str, campaign: str | None) -> pd.DataFrame:
    df = _read_csv_at(camp_fd, fname)
    if df.empty:
        _fail(f"mitad vacía: {fname}")
    if tuple(df.columns) != _POOL_COLS:
        _fail(f"{fname} con columnas {list(df.columns)} != las 19 canónicas en orden")
    # B86-style: isna ANTES de astype (NaN→"nan" enmascararía el vacío).
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
        # B85: NaN REAL (celda vacía = modelo fallido) permitido; TEXTO no numérico coercionado a NaN, NO.
        if (raw.notna() & v.isna()).any():
            _fail(f"{fname} columna {c} con texto no numérico")
        if ((v == math.inf) | (v == -math.inf)).any():
            _fail(f"{fname} columna {c} con valor infinito")
    secs = pd.to_numeric(df["secs"], errors="coerce")
    if secs.isna().any() or (secs < 0).any():
        _fail(f"{fname} columna secs ausente o negativa")
    return df


class RollbackError(OSError):
    """B104: fallo ANTES del punto de commit — la transacción se REVIRTIÓ (outputs previos restaurados o
    recuperación verificable materializada). Reintentar es seguro. Antes se usaba un `OSError` genérico."""


class CommittedStateError(RuntimeError):
    """B104: el punto de commit SÍ se cruzó — los outputs nuevos son la AUTORIDAD y son durables — pero la
    limpieza/fsync posterior quedó incompleta. NUNCA confundir con un rollback: reintentar a ciegas es
    incorrecto (los outputs ya cambiaron)."""


def _digest_fd(fd: int) -> str:
    """sha256 del contenido leído DEL descriptor (no del nombre) — el fd apunta al inode que la transacción creó."""
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while chunk := os.read(fd, 1 << 16):
        h.update(chunk)
    return h.hexdigest()


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
    El nombre NO se 'registra' antes del open — si el open falla (colisión/symlink), no queda estado que un
    rollback pudiera borrar por error. Devuelve (name, fd r/w VIVO)."""
    name = f".{base}.{kind}.{os.getpid()}.{i}.{secrets.token_hex(8)}"
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    return name, fd


class _Out:
    """Estado transaccional por output con PROPIEDAD por descriptor (B100–B103): un temporal/backup pertenece a
    la transacción SOLO cuando su `O_EXCL` open devolvió un fd (`*_created=True`), ese fd pasa `fstat` y su
    `*_name` liga al mismo dev/ino. `*_name` por sí solo NO es propiedad."""

    __slots__ = (
        "dir_fd", "label", "name", "df",
        "existed_before", "previous_bytes", "previous_digest",
        "temp_created", "temp_name", "temp_fd", "temp_digest",
        "backup_created", "backup_name", "backup_fd", "backup_digest",
        "promoted", "recovered", "recovery_name",
    )  # fmt: skip

    def __init__(self, dir_fd: int, label: str, name: str, df: pd.DataFrame) -> None:
        self.dir_fd = dir_fd
        self.label = label
        self.name = name
        self.df = df
        self.existed_before = False
        self.previous_bytes: bytes | None = None
        self.previous_digest: str | None = None
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
        self.recovery_name: str | None = None

    def close_fds(self, errs: list[str]) -> None:
        for fd_attr in ("temp_fd", "backup_fd"):
            fd = getattr(self, fd_attr)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as exc:
                    errs.append(f"cerrar {fd_attr} de {self.name!r}: {exc}")
                setattr(self, fd_attr, -1)


def _safe_unlink_bound(o: _Out, name: str | None, fd: int, errs: list[str]) -> None:
    """Borra `name` SOLO si sigue ligado al `fd` que la transacción creó (B100: nunca borra un objeto ajeno que
    haya tomado el nombre). Ausencia = ya no está, nada que borrar."""
    if name is None or fd < 0:
        return
    if _binding_problem(o.dir_fd, name, fd, mode=0o600) is not None:
        return  # el nombre no liga a NUESTRO fd → objeto ajeno, no se toca
    try:
        os.unlink(name, dir_fd=o.dir_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        errs.append(f"unlink {name!r}: {exc}")


def _recover_from_bytes(o: _Out, errs: list[str]) -> str | None:
    """B98/B103: materializa una recuperación VERIFICABLE del output previo desde `previous_bytes` (copia de
    confianza en memoria) cuando el backup en disco no es fiable. Escribe un artefacto nuevo (O_EXCL), fsync,
    digest == previous_digest, intenta promoverlo al target y VERIFICA; si la promoción falla lo conserva con
    su nombre de recuperación aleatorio, reabriéndolo para confirmar identidad+digest. Devuelve la ruta
    relativa de la recuperación confirmada o None."""
    if o.previous_bytes is None or o.previous_digest is None:
        errs.append(f"sin bytes previos de confianza para recuperar {o.name!r}")
        return None
    try:
        rname, rfd = _create_governed(o.dir_fd, o.name, "rec", 0)
    except OSError as exc:
        errs.append(f"crear recuperación de {o.name!r}: {exc}")
        return None
    try:
        with os.fdopen(rfd, "wb", closefd=False) as rf:
            rf.write(o.previous_bytes)
            rf.flush()
            os.fsync(rf.fileno())
        if _digest_fd(rfd) != o.previous_digest:
            errs.append(f"recuperación de {o.name!r} con digest inconsistente")
            return None
        try:  # intenta promover la recuperación al target y verificar
            os.replace(rname, o.name, src_dir_fd=o.dir_fd, dst_dir_fd=o.dir_fd)
            if _binding_problem(o.dir_fd, o.name, rfd, mode=0o600) is None and _digest_fd(rfd) == o.previous_digest:
                os.fsync(o.dir_fd)
                o.recovered = True
                return f"reports/{o.label}/{o.name}"
            errs.append(f"recuperación promovida de {o.name!r} no verifica")
            return None
        except OSError:
            os.fsync(o.dir_fd)  # conserva la recuperación bajo su nombre aleatorio y CONFIRMA que existe
            if _binding_problem(o.dir_fd, rname, rfd, mode=0o600) is None and _digest_fd(rfd) == o.previous_digest:
                o.recovery_name = rname
                return f"reports/{o.label}/{rname}"
            errs.append(f"recuperación de {o.name!r} no verificable en disco")
            return None
    finally:
        try:
            os.close(rfd)
        except OSError:
            pass


def _promote_transactionally(chain: _Chain, outs: list[_Out]) -> None:
    """Transacción con propiedad por descriptor (B90/B92/B94/B97/B98/B99/B100–B104). Secuencia (Fase 7):
    prepara temps (O_EXCL, fd vivo, digest) → prepara backups (bytes de confianza + backup fd/digest) →
    verifica bindings+digests → reverify → promueve → verifica target↔temp_fd+digest → reverify → fsync →
    reverify FINAL (punto de commit, backups presentes) → COMMIT → limpia backups ligados → fsync. Un fallo
    ANTES del commit ⇒ `_rollback` (restaura por backup ligado+digest o recupera desde bytes de confianza,
    RollbackError). Un fallo DESPUÉS del commit ⇒ `CommittedStateError` (nunca ambiguo). Los fds se cierran en
    un `finally` único (Fase 8)."""

    def _fsync_dirs() -> None:
        os.fsync(chain.camp)
        os.fsync(chain.ev)

    def _rollback(original: BaseException) -> NoReturn:
        errs: list[str] = []
        preserved: list[str] = []
        for o in outs:  # deshace promociones
            if not o.promoted:
                continue
            if not o.existed_before:  # ausente antes → elimina el nuevo SOLO si liga a NUESTRO temporal
                if _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600) is None:
                    _safe_unlink_bound(o, o.name, o.temp_fd, errs)
                else:
                    errs.append(f"{o.name!r} tras promover no liga al temporal creado; NO se elimina objeto ajeno")
                continue
            restored = False  # existía antes → restaura desde el backup SOLO si liga a NUESTRO fd Y digest OK
            if o.backup_created and o.backup_name is not None:
                bprob = _binding_problem(o.dir_fd, o.backup_name, o.backup_fd, mode=0o600)
                if bprob is None and _digest_fd(o.backup_fd) == o.previous_digest:
                    try:
                        os.replace(o.backup_name, o.name, src_dir_fd=o.dir_fd, dst_dir_fd=o.dir_fd)
                        vfd = os.open(o.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=o.dir_fd)
                        try:
                            restored = _digest_fd(vfd) == o.previous_digest
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
            if not restored:  # backup ausente/sustituido/inconsistente → recupera desde bytes de confianza
                rec = _recover_from_bytes(o, errs)
                if rec is not None:
                    preserved.append(f"RECUPERACIÓN PRESERVADA {rec} (de {o.name!r})")
                else:
                    errs.append(f"NO se pudo recuperar {o.name!r}")
        for o in outs:  # borra temporales SOLO si ligan a NUESTRO fd (B100)
            _safe_unlink_bound(o, o.temp_name, o.temp_fd, errs)
        for o in outs:  # backups: CONSERVA solo la última copia recuperable (promovido+existía+no-recuperado)
            if o.promoted and o.existed_before and not o.recovered:
                continue  # B98/Fase 6.5: no borrar un backup cuya restauración/recuperación no se confirmó
            _safe_unlink_bound(o, o.backup_name, o.backup_fd, errs)
        try:
            _fsync_dirs()  # B92
        except OSError as exc:
            errs.append(f"fsync de directorios: {exc}")
        raise RollbackError(f"{original!r}; recuperaciones: {preserved}; errores de rollback: {errs}") from original

    try:
        for i, o in enumerate(outs):  # 1. temporales: O_EXCL → registra fd → escribe → fsync → digest
            data = o.df.to_csv(index=False).encode()
            o.temp_name, o.temp_fd = _create_governed(o.dir_fd, o.name, "tmp", i)
            o.temp_created = True
            with os.fdopen(o.temp_fd, "wb", closefd=False) as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            o.temp_digest = _digest_fd(o.temp_fd)
        for i, o in enumerate(outs):  # 2. respaldos: lee source de confianza → O_EXCL → escribe → fsync → digest
            try:
                sfd = os.open(o.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=o.dir_fd)
            except FileNotFoundError:
                o.existed_before = False
                continue
            except OSError as exc:
                _fail(f"output previo {o.name!r} inabrible (symlink plantado: {exc})")
            try:
                src_prob = _fd_governed(sfd, mode=None)
                if src_prob is None and (stat.S_IMODE(os.fstat(sfd).st_mode) & 0o022):
                    src_prob = "escribible por grupo/otros"
                if src_prob is not None:
                    _fail(f"output previo {o.name!r}: {src_prob}")
                with os.fdopen(sfd, "rb", closefd=False) as sfh:
                    o.previous_bytes = sfh.read()
            finally:
                os.close(sfd)
            o.existed_before = True
            o.previous_digest = hashlib.sha256(o.previous_bytes).hexdigest()
            o.backup_name, o.backup_fd = _create_governed(o.dir_fd, o.name, "bak", i)
            o.backup_created = True
            with os.fdopen(o.backup_fd, "wb", closefd=False) as bfh:
                bfh.write(o.previous_bytes)
                bfh.flush()
                os.fsync(bfh.fileno())
            o.backup_digest = _digest_fd(o.backup_fd)
            if o.backup_digest != o.previous_digest:
                _fail(f"backup de {o.name!r} no coincide con el output previo (digest)")
        chain.reverify("antes de promover")  # B90
        for o in outs:  # 3. verifica binding + digest del temporal ANTES de promover (B102)
            assert o.temp_name is not None
            prob = _binding_problem(o.dir_fd, o.temp_name, o.temp_fd, mode=0o600)
            if prob is not None:
                _fail(f"temporal de {o.name!r} comprometido antes de promover: {prob}")
            if _digest_fd(o.temp_fd) != o.temp_digest:
                _fail(f"temporal de {o.name!r} mutado antes de promover (digest)")
        for o in outs:  # 4. promueve (atómico por fichero, fd-relativo)
            assert o.temp_name is not None
            os.replace(o.temp_name, o.name, src_dir_fd=o.dir_fd, dst_dir_fd=o.dir_fd)
            o.promoted = True
        for o in outs:  # 5. verifica que el target liga al temporal que creamos + digest (B102)
            prob = _binding_problem(o.dir_fd, o.name, o.temp_fd, mode=0o600)
            if prob is not None:
                _fail(f"output {o.name!r} tras promover no liga al temporal creado: {prob}")
            if _digest_fd(o.temp_fd) != o.temp_digest:
                _fail(f"output {o.name!r} tras promover con digest distinto")
        chain.reverify("después de promover")
        _fsync_dirs()
        chain.reverify("punto de commit")  # B99: reverify FINAL con los backups AÚN presentes
    except BaseException as original:
        _rollback(original)  # NoReturn: RollbackError o re-eleva
    # ---- PUNTO DE COMMIT ---- (outputs nuevos = autoridad y durables; de aquí en adelante: CommittedStateError)
    cleanup_errors: list[str] = []
    for o in outs:  # limpia backups SOLO si su nombre sigue ligado a NUESTRO fd (B100)
        _safe_unlink_bound(o, o.backup_name, o.backup_fd, cleanup_errors)
    try:
        _fsync_dirs()
    except OSError as exc:  # B104: post-commit ⇒ estado comprometido, jamás un OSError ambiguo
        raise CommittedStateError(
            f"COMMIT CRUZADO (outputs nuevos son la autoridad y son durables) pero el fsync post-commit falló: "
            f"{exc}. NO reintentar como si hubiera rollback."
        ) from exc
    if cleanup_errors:
        raise CommittedStateError(
            f"COMMIT CRUZADO (outputs nuevos son la autoridad) pero la limpieza de backups quedó incompleta: "
            f"{cleanup_errors}. NO reintentar como si hubiera rollback."
        )


def merge() -> int:
    campaign = os.environ.get("CAMPAIGN_ID")
    if campaign is not None and not campaign.strip():
        _fail("CAMPAIGN_ID definido pero vacío")
    chain = _Chain()  # B90: cadena gobernada abierta ANTES de tocar nada (cwd = ROOT)
    lock_fd = -1
    outs: list[_Out] = []
    try:
        lock_fd = _acquire_lock(chain.camp)  # B89: exclusión; los inputs se validan BAJO el lock
        chain.reverify("tras adquirir el lock")
        # 1. Carga + valida las OCHO mitades ANTES de escribir nada (todo-o-nada), bajo el lock, fd-bound.
        for table in _TABLES:
            for block in _BLOCKS:
                parts = [
                    _load_half(chain.camp, f"aq_pool_{kind}_{table}_{block}.csv", table, campaign) for kind in _HALVES
                ]
                full = pd.concat(parts, ignore_index=True)
                full["source_run_id"] = full["run_id"]
                # B85: bajo campaña el run_id ES la campaña; standalone = máximo LEXICOGRÁFICO (string).
                full["run_id"] = campaign if campaign is not None else str(full["run_id"].astype(str).max())
                tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
                outs.append(_Out(chain.camp, "campaign", f"campaign_pool_{table}_{block}.csv", full))
                outs.append(_Out(chain.ev, "eval", tgt, full))
                print(f"{table}/{block}: {len(full)} rows -> {tgt}")
        # 2. Promoción transaccional fd-relativa (B99: la reverify FINAL vive DENTRO, antes del punto de commit —
        # no hay reverify posterior a la destrucción de backups que pudiera detectar un swap sin poder revertir).
        _promote_transactionally(chain, outs)
    finally:  # Fase 8: cierre único de TODOS los descriptores (artefactos → lock → cadena) sin ocultar el resultado
        close_errs: list[str] = []
        for o in outs:
            o.close_fds(close_errs)
        if lock_fd >= 0:
            os.close(lock_fd)
        chain.close()
        if close_errs:
            print(f"merge_campaign_pools: avisos de cierre de descriptores: {close_errs}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(merge())
