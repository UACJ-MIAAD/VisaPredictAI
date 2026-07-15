#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74/B79/B80 — extraído del heredoc de run_campaign_aq{,_tail}.sh).

FAIL-CLOSED sobre el esquema REAL de producción (B79/B80): exige las OCHO mitades exactas (2 tablas × 2
bloques × 2 mitades), cada una fichero REGULAR (no symlink), con EXACTAMENTE las 19 columnas canónicas en
orden, un ÚNICO `run_id` no vacío tratado como STRING (nunca numérico — los reales son `20260706T114535-<sha>`),
`table` coincidente con el nombre del fichero, `model`/`country`/`category` strings no vacíos, métricas finitas
(NaN permitido para modelos fallidos, infinito NO) y `secs` numérico ≥ 0. El `run_id` de salida es el MÁXIMO
LEXICOGRÁFICO de las mitades; el original sobrevive en `source_run_id`.

Garantía de escritura (honesta): **validación GLOBAL previa, promoción ATÓMICA por fichero y ROLLBACK
transaccional ante errores recuperables** (respalda los outputs previos y los restaura byte-idénticos si una
promoción falla). NO es atomicidad de bundle crash-safe (un kill a mitad puede dejar estado parcial); esa
garantía, con manifiesto final, vive en P2b antes de F2.
"""

from __future__ import annotations

import math
import os
import pathlib
import shutil
import sys
import tempfile

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


def _fail(msg: str) -> None:
    print(f"merge_campaign_pools: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _load_half(f: pathlib.Path, table: str, block: str, kind: str) -> pd.DataFrame:
    if f.is_symlink() or not f.is_file():
        _fail(f"mitad {kind} de {table}/{block} ausente o no-regular: {f}")
    # run_id SIEMPRE como string (los reales son `20260706T114535-<sha>`, no numéricos).
    df = pd.read_csv(f, dtype={"run_id": str})
    if df.empty:
        _fail(f"mitad vacía: {f}")
    if tuple(df.columns) != _POOL_COLS:
        _fail(f"{f} con columnas {list(df.columns)} != las 19 canónicas en orden")
    rid = df["run_id"].astype(str)
    if rid.isna().any() or (rid.str.strip() == "").any() or (rid == "nan").any():
        _fail(f"run_id vacío en {f}")
    if rid.nunique() != 1:
        _fail(f"{f} con múltiples run_id ({rid.nunique()}) en una sola mitad")
    if (df["table"].astype(str) != table).any():
        _fail(f"{f} columna table != {table} del nombre de fichero")
    for c in _STR_COLS:
        s = df[c].astype(str)
        if s.isna().any() or (s.str.strip() == "").any():
            _fail(f"{f} columna {c} con valores vacíos")
    for c in _METRIC_COLS:
        v = pd.to_numeric(df[c], errors="coerce")
        # NaN permitido (modelo fallido); infinito NO.
        if (
            v.apply(lambda x: not (math.isnan(x) or math.isfinite(x))).any()
            or (v == math.inf).any()
            or (v == -math.inf).any()
        ):
            _fail(f"{f} columna {c} con valor infinito")
    secs = pd.to_numeric(df["secs"], errors="coerce")
    if secs.isna().any() or (secs < 0).any() or not secs.apply(math.isfinite).all():
        _fail(f"{f} columna secs no-finita o negativa")
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
    writes: list[tuple[pathlib.Path, pd.DataFrame]] = []
    # 1. Carga + valida las OCHO mitades ANTES de escribir nada (todo-o-nada).
    for table in _TABLES:
        for block in _BLOCKS:
            parts = [_load_half(camp / f"aq_pool_{kind}_{table}_{block}.csv", table, block, kind) for kind in _HALVES]
            full = pd.concat(parts, ignore_index=True)
            full["source_run_id"] = full["run_id"]
            full["run_id"] = str(full["run_id"].astype(str).max())  # máximo LEXICOGRÁFICO (string)
            tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
            writes.append((camp / f"campaign_pool_{table}_{block}.csv", full))
            writes.append((ev / tgt, full))
            print(f"{table}/{block}: {len(full)} rows -> {tgt}")
    # 2. Promoción transaccional de los ocho outputs (validación global ya hecha).
    _promote_transactionally(writes)
    return 0


if __name__ == "__main__":
    sys.exit(merge())
