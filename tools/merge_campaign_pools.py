#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74/B79/B80/B85/B89 — extraído del heredoc de run_campaign_aq{,_tail}.sh).

FAIL-CLOSED sobre el esquema REAL de producción (B79/B80/B85): exige las OCHO mitades exactas (2 tablas × 2
bloques × 2 mitades), cada una fichero REGULAR (no symlink), con EXACTAMENTE las 19 columnas canónicas en
orden, un ÚNICO `run_id` no vacío tratado como STRING (nunca numérico — los reales son `20260706T114535-<sha>`),
`table` coincidente con el nombre del fichero, `model`/`country`/`category` strings no vacíos (isna se evalúa
ANTES de astype: NaN→"nan" enmascara el vacío), y métricas donde se distingue el NaN REAL (celda vacía =
modelo fallido, permitido) del TEXTO no numérico coercionado a NaN (bloqueado) y del infinito (bloqueado);
`secs` numérico ≥ 0. **Identidad de campaña (B85):** si `CAMPAIGN_ID` está en el entorno (el runbook la
exporta y `vp_model.config` la pinea como run_id), las OCHO mitades deben llevar EXACTAMENTE ese run_id — no
se fusionan campañas mezcladas; el run_id de salida es la campaña. En modo standalone (sin `CAMPAIGN_ID`) el
run_id de salida es el MÁXIMO LEXICOGRÁFICO de las mitades; el original sobrevive en `source_run_id`.

Exclusión concurrente (B89): un lock gobernado (`reports/campaign/.merge.lock`, 0600, O_NOFOLLOW, regular,
del UID, nlink==1) con `flock LOCK_EX` BLOQUEANTE se sostiene durante carga+validación+respaldo+promoción+
rollback+fsync — los inputs se validan BAJO el lock y dos merges concurrentes se serializan.

Garantía de escritura (honesta): **validación GLOBAL previa, promoción ATÓMICA por fichero y ROLLBACK
transaccional ante errores recuperables** (respalda los outputs previos y los restaura byte-idénticos si una
promoción falla). NO es atomicidad de bundle crash-safe (un kill a mitad puede dejar estado parcial); esa
garantía, con manifiesto final, vive en P2b antes de F2.
"""

from __future__ import annotations

import fcntl
import math
import os
import pathlib
import shutil
import stat
import sys
import tempfile
from typing import NoReturn

import pandas as pd

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


def _fail(msg: str) -> NoReturn:
    print(f"merge_campaign_pools: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _acquire_lock(camp: pathlib.Path) -> int:
    """B89: exclusión entre merges concurrentes. Abre/crea el lock gobernado (0600 vía fchmod inmune al
    umask, O_NOFOLLOW ⇒ symlink revienta) y valida por fstat: regular, del UID, nlink==1, permisos 0600
    exactos. `flock LOCK_EX` BLOQUEANTE: el segundo proceso espera y después opera sobre estado completo."""
    p = camp / _LOCK_NAME
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
    except FileExistsError:
        try:
            fd = os.open(str(p), os.O_RDWR | os.O_NOFOLLOW)
        except OSError as exc:
            _fail(f"lock de merge inabrible ({exc})")
    except OSError as exc:
        _fail(f"lock de merge no creable ({exc})")
    st = os.fstat(fd)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.geteuid()
        or st.st_nlink != 1
        or stat.S_IMODE(st.st_mode) != 0o600
    ):
        os.close(fd)
        _fail(f"lock de merge no-regular/ajeno/hardlink/permisos: {p}")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _load_half(f: pathlib.Path, table: str, block: str, kind: str, campaign: str | None) -> pd.DataFrame:
    if f.is_symlink() or not f.is_file():
        _fail(f"mitad {kind} de {table}/{block} ausente o no-regular: {f}")
    # run_id SIEMPRE como string (los reales son `20260706T114535-<sha>`, no numéricos).
    df = pd.read_csv(f, dtype={"run_id": str})
    if df.empty:
        _fail(f"mitad vacía: {f}")
    if tuple(df.columns) != _POOL_COLS:
        _fail(f"{f} con columnas {list(df.columns)} != las 19 canónicas en orden")
    # B86-style: isna ANTES de astype (NaN→"nan" enmascararía el vacío).
    if df["run_id"].isna().any():
        _fail(f"run_id vacío en {f}")
    rid = df["run_id"].astype(str)
    if (rid.str.strip() == "").any():
        _fail(f"run_id vacío en {f}")
    if rid.nunique() != 1:
        _fail(f"{f} con múltiples run_id ({rid.nunique()}) en una sola mitad")
    if campaign is not None and rid.iloc[0] != campaign:
        _fail(f"{f} run_id {rid.iloc[0]!r} != CAMPAIGN_ID {campaign!r} (mezcla de campañas prohibida)")
    if (df["table"].astype(str) != table).any():
        _fail(f"{f} columna table != {table} del nombre de fichero")
    for c in _STR_COLS:
        if df[c].isna().any():
            _fail(f"{f} columna {c} con valores ausentes")
        if (df[c].astype(str).str.strip() == "").any():
            _fail(f"{f} columna {c} con valores vacíos")
    for c in _METRIC_COLS:
        raw = df[c]
        v = pd.to_numeric(raw, errors="coerce")
        # B85: NaN REAL (celda vacía = modelo fallido) permitido; TEXTO no numérico coercionado a NaN, NO.
        if (raw.notna() & v.isna()).any():
            _fail(f"{f} columna {c} con texto no numérico")
        if ((v == math.inf) | (v == -math.inf)).any():
            _fail(f"{f} columna {c} con valor infinito")
    secs = pd.to_numeric(df["secs"], errors="coerce")
    if secs.isna().any() or (secs < 0).any():
        _fail(f"{f} columna secs ausente o negativa")
    return df


def _promote_transactionally(writes: list[tuple[pathlib.Path, pd.DataFrame]]) -> None:
    """Escribe 8 temporales (fsync), respalda los outputs existentes, promueve por `os.replace` y, si una
    promoción falla, RESTAURA todos los outputs previos byte-idénticos. Fsync de los directorios al final."""
    temps: list[tuple[pathlib.Path, pathlib.Path]] = []
    backups: dict[pathlib.Path, pathlib.Path | None] = {}
    promoted: list[pathlib.Path] = []
    try:
        for path, df in writes:  # 1. prepara + fsync TODOS los temporales antes de promover ninguno
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                df.to_csv(fh, index=False)
                fh.flush()
                os.fsync(fh.fileno())
            temps.append((path, pathlib.Path(tmp)))
        for path, _ in writes:  # 2. respalda los outputs existentes
            if path.exists():
                bfd, bpath = tempfile.mkstemp(dir=str(path.parent), prefix=f".bak.{path.name}.")
                os.close(bfd)
                shutil.copy2(path, bpath)
                backups[path] = pathlib.Path(bpath)
            else:
                backups[path] = None
        for path, tpath in temps:  # 3. promueve (atómico por fichero)
            os.replace(tpath, path)
            promoted.append(path)
    except BaseException:  # rollback transaccional: restaura los outputs previos
        for path in promoted:
            b = backups.get(path)
            if b is not None:
                os.replace(b, path)
            else:
                path.unlink(missing_ok=True)
        for _p, tpath in temps:
            tpath.unlink(missing_ok=True)
        for b in backups.values():
            if b is not None:
                b.unlink(missing_ok=True)
        raise
    for b in backups.values():  # éxito: limpia respaldos
        if b is not None:
            b.unlink(missing_ok=True)
    for d in {path.parent for path, _ in writes}:  # fsync de los directorios
        dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)


def merge() -> int:
    camp = pathlib.Path("reports/campaign")
    ev = pathlib.Path("reports/eval")
    campaign = os.environ.get("CAMPAIGN_ID")
    if campaign is not None and not campaign.strip():
        _fail("CAMPAIGN_ID definido pero vacío")
    lock_fd = _acquire_lock(camp)  # B89: exclusión; los inputs se validan BAJO el lock
    try:
        writes: list[tuple[pathlib.Path, pd.DataFrame]] = []
        # 1. Carga + valida las OCHO mitades ANTES de escribir nada (todo-o-nada), bajo el lock.
        for table in _TABLES:
            for block in _BLOCKS:
                parts = [
                    _load_half(camp / f"aq_pool_{kind}_{table}_{block}.csv", table, block, kind, campaign)
                    for kind in _HALVES
                ]
                full = pd.concat(parts, ignore_index=True)
                full["source_run_id"] = full["run_id"]
                # B85: bajo campaña el run_id ES la campaña; standalone = máximo LEXICOGRÁFICO (string).
                full["run_id"] = campaign if campaign is not None else str(full["run_id"].astype(str).max())
                tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
                writes.append((camp / f"campaign_pool_{table}_{block}.csv", full))
                writes.append((ev / tgt, full))
                print(f"{table}/{block}: {len(full)} rows -> {tgt}")
        # 2. Promoción transaccional de los ocho outputs (validación global ya hecha).
        _promote_transactionally(writes)
    finally:
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(merge())
