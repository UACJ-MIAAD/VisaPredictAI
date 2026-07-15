#!/usr/bin/env python
"""Fusiona las mitades del pool de campaña (nongbm + gbm) en `campaign_pool_*` y proyecta a
`model_comparison_*` (P0R.5 · R9.4/B66/B74 — extraído del heredoc de run_campaign_aq{,_tail}.sh).

FAIL-CLOSED (B74): exige las OCHO mitades exactas (2 tablas × 2 bloques × 2 mitades), cada una fichero regular
no vacío, con las columnas requeridas y el MISMO esquema entre mitades, `run_id` no nulo y finito. Prepara y
valida TODO antes de escribir nada; escribe de forma ATÓMICA (temp 0600 + fsync + os.replace). Ante cualquier
error: exit ≠ 0 y CERO outputs nuevos/parciales.

UNA campaña = UN run_id: 9 consumidores aguas abajo filtran por `run_id==max()`. El id por-mitad sobrevive en
`source_run_id`.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import numpy as np
import pandas as pd

_TABLES = ("FAD", "DFF")
_BLOCKS = ("family", "employment")
_HALVES = ("nongbm", "gbm")
_REQUIRED_COLS = {"run_id", "model"}


def _fail(msg: str) -> None:
    print(f"merge_campaign_pools: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _atomic_write_csv(path: pathlib.Path, df: pd.DataFrame) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            df.to_csv(fh, index=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def merge() -> int:
    camp = pathlib.Path("reports/campaign")
    ev = pathlib.Path("reports/eval")
    # 1. Carga + valida las OCHO mitades ANTES de escribir nada (todo-o-nada).
    prepared: dict[tuple[str, str], pd.DataFrame] = {}
    for table in _TABLES:
        for block in _BLOCKS:
            parts: list[pd.DataFrame] = []
            cols: list[str] | None = None
            for kind in _HALVES:
                f = camp / f"aq_pool_{kind}_{table}_{block}.csv"
                if not f.is_file():
                    _fail(f"falta la mitad {kind} de {table}/{block}: {f}")
                df = pd.read_csv(f)
                if df.empty:
                    _fail(f"mitad vacía: {f}")
                missing = _REQUIRED_COLS - set(df.columns)
                if missing:
                    _fail(f"{f} sin columnas requeridas {sorted(missing)}")
                if cols is None:
                    cols = list(df.columns)
                elif list(df.columns) != cols:
                    _fail(f"esquema distinto entre mitades de {table}/{block}")
                rid = pd.to_numeric(df["run_id"], errors="coerce")
                if rid.isna().any() or not np.isfinite(rid).all():
                    _fail(f"run_id nulo/no-finito en {f}")
                parts.append(df)
            full = pd.concat(parts, ignore_index=True)
            full["source_run_id"] = full["run_id"]
            full["run_id"] = full["run_id"].max()
            prepared[(table, block)] = full
    # 2. Escritura ATÓMICA de TODO, solo tras validar las ocho.
    for (table, block), full in prepared.items():
        _atomic_write_csv(camp / f"campaign_pool_{table}_{block}.csv", full)
        tgt = f"model_comparison_{table}21.csv" if block == "family" else f"model_comparison_EB_{table}21.csv"
        _atomic_write_csv(ev / tgt, full)
        print(f"{table}/{block}: {len(full)} rows -> {tgt}")
    return 0


if __name__ == "__main__":
    sys.exit(merge())
