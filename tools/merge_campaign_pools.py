#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74/B79/B80/B85/B89/B90/B92/B94/B95 — extraído del heredoc de
run_campaign_aq{,_tail}.sh). Se invoca desde ROOT (run-command fija cwd=root).

Lectura de mitades gobernada (B95): cada CSV se lee por `governed_read.read_governed_csv` — snapshot `fstat`
pre/post exacto (regular/UID/nlink==1/**sin escritura de grupo-otros**/dev·ino·size·mtime·ctime estables) del
MISMO descriptor. Limpieza ESTRICTA (B94): un fallo al borrar backups/temporales NO deja devolver éxito y en
rollback se adjunta al error original (nada de OSError silenciado).

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

Garantías de escritura (honestas): validación GLOBAL previa; promoción ATÓMICA por fichero; **ROLLBACK
transaccional DURABLE** ante errores recuperables (restaura los outputs previos byte-idénticos y hace fsync de
AMBOS directorios también en el camino de error — B92). NO es atomicidad de bundle crash-safe (un kill a mitad
puede dejar estado parcial); esa garantía, con manifiesto final, vive en P2b antes de F2.
"""

from __future__ import annotations

import fcntl
import math
import os
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


def _unlink_strict(name: str, dir_fd: int, errors: list[str]) -> None:
    """B94: borra RELATIVO al descriptor; una ausencia esperable (FileNotFoundError) se tolera, pero
    PermissionError/EIO/cualquier otro OSError se RECOPILA (jamás se silencia)."""
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors.append(f"unlink {name!r}: {exc}")


def _promote_transactionally(chain: _Chain, writes: list[tuple[int, str, pd.DataFrame]]) -> None:
    """Todo fd-relativo (B90): temporales y respaldos con nombres RELATIVOS + O_CREAT|O_EXCL|O_NOFOLLOW dentro
    del descriptor destino; promoción/restauración por `os.replace(src_dir_fd=…, dst_dir_fd=…)`; identidad de
    la cadena reverificada ANTES y DESPUÉS de promover. Fsync de campaign y eval en éxito Y tras rollback
    (B92: el rollback también es durable). B94: la limpieza es ESTRICTA — un fallo al borrar backups/temporales
    (residuo) NO deja devolver éxito y en rollback se agrega al error original."""
    temps: list[tuple[int, str, str]] = []
    backups: dict[tuple[int, str], str | None] = {}
    promoted: list[tuple[int, str]] = []

    def _fsync_dirs() -> None:
        os.fsync(chain.camp)
        os.fsync(chain.ev)

    try:
        for i, (dfd, name, df) in enumerate(writes):  # 1. temporales fsync'd ANTES de promover ninguno
            tname = f".{name}.tmp.{os.getpid()}.{i}"
            tfd = os.open(tname, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(tfd, "w") as fh:
                df.to_csv(fh, index=False)
                fh.flush()
                os.fsync(fh.fileno())
            temps.append((dfd, name, tname))
        for i, (dfd, name, _df) in enumerate(writes):  # 2. respaldos fd-relativos de los outputs existentes
            key = (dfd, name)
            try:
                sfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            except FileNotFoundError:
                backups[key] = None
                continue
            except OSError as exc:
                _fail(f"output previo {name!r} inabrible (symlink plantado: {exc})")
            sst = os.fstat(sfd)
            if not stat.S_ISREG(sst.st_mode):
                os.close(sfd)
                _fail(f"output previo {name!r} no-regular")
            with os.fdopen(sfd, "rb") as sfh:
                data = sfh.read()
            bname = f".bak.{name}.{os.getpid()}.{i}"
            bfd = os.open(bname, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(bfd, "wb") as bfh:
                bfh.write(data)
                bfh.flush()
                os.fsync(bfh.fileno())
            backups[key] = bname
        chain.reverify("antes de promover")  # B90: la cadena sigue siendo LA validada
        for dfd, name, tname in temps:  # 3. promueve (atómico por fichero, fd-relativo)
            os.replace(tname, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            promoted.append((dfd, name))
        chain.reverify("después de promover")
    except BaseException as original:  # rollback transaccional fd-relativo: restaura los outputs previos
        rb_errors: list[str] = []
        for dfd, name in promoted:
            b = backups.get((dfd, name))
            try:
                if b is not None:
                    os.replace(b, name, src_dir_fd=dfd, dst_dir_fd=dfd)
                else:
                    os.unlink(name, dir_fd=dfd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                rb_errors.append(f"restaurar {name!r}: {exc}")
        for dfd, _name, tname in temps:
            _unlink_strict(tname, dfd, rb_errors)
        for (dfd, _n), b in backups.items():
            if b is not None:
                _unlink_strict(b, dfd, rb_errors)
        try:
            _fsync_dirs()  # B92: durabilidad TAMBIÉN en el camino de error
        except OSError as exc:
            rb_errors.append(f"fsync de directorios: {exc}")
        if rb_errors:  # B94: adjunta los fallos de rollback/limpieza al error original
            raise OSError(f"{original!r}; además fallos de rollback/limpieza: {rb_errors}") from original
        raise
    cleanup_errors: list[str] = []
    for (dfd, _n), b in backups.items():  # éxito: limpia respaldos ESTRICTAMENTE
        if b is not None:
            _unlink_strict(b, dfd, cleanup_errors)
    _fsync_dirs()
    if cleanup_errors:  # B94: éxito ⇒ CERO residuos; un backup que no se borró NO es éxito
        _fail(f"promoción OK pero la limpieza de respaldos falló (residuos): {cleanup_errors}")


def merge() -> int:
    campaign = os.environ.get("CAMPAIGN_ID")
    if campaign is not None and not campaign.strip():
        _fail("CAMPAIGN_ID definido pero vacío")
    chain = _Chain()  # B90: cadena gobernada abierta ANTES de tocar nada (cwd = ROOT)
    lock_fd = -1
    try:
        lock_fd = _acquire_lock(chain.camp)  # B89: exclusión; los inputs se validan BAJO el lock
        chain.reverify("tras adquirir el lock")
        writes: list[tuple[int, str, pd.DataFrame]] = []
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
                writes.append((chain.camp, f"campaign_pool_{table}_{block}.csv", full))
                writes.append((chain.ev, tgt, full))
                print(f"{table}/{block}: {len(full)} rows -> {tgt}")
        # 2. Promoción transaccional fd-relativa de los ocho outputs (validación global ya hecha).
        _promote_transactionally(chain, writes)
        chain.reverify("antes de devolver éxito")
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        chain.close()
    return 0


if __name__ == "__main__":
    sys.exit(merge())
