#!/usr/bin/env python
"""¿Los 5 refits deep FAD camp_auto están COMPLETOS y CONSISTENTES? (P0R.5 · R9.4/B66/B74/B81 — extraído del
heredoc de run_campaign_aq_tail.sh). Exit 0 solo si las CINCO semillas s1…s5 cumplen, ANTE evidencia posible-
mente alterada: fichero REGULAR (no symlink), no vacío, columnas `unique_id`/`ds`/`y`/`AutoBiTCN` (formato
ancho de NeuralForecast), `unique_id` no vacío, `ds` parseable, `y` y `AutoBiTCN` numéricos y finitos,
`(unique_id, ds)` ÚNICO dentro de cada semilla, y el conjunto ORDENADO de `(unique_id, ds, y)` IDÉNTICO
(mismo número de filas y mismas claves) entre las cinco. Exit 1 ante cualquier ausencia/inconsistencia (⇒ el
runbook re-corre los 5 refits). Sin efectos secundarios.

B74/B81: el heredoc original solo miraba `s1` y una columna `model` inexistente; una versión intermedia usaba
`set()` sobre las claves, perdiendo la MULTIPLICIDAD (una semilla con filas duplicadas pasaba). Aquí se compara
por DataFrame ORDENADO y se exige unicidad. (El contrato explícito de elegibilidad 580/600 vive en P2b.)"""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

_SEEDS = (1, 2, 3, 4, 5)
_REQUIRED_COLS = ("unique_id", "ds", "y", "AutoBiTCN")


def _seed_keys(f: pathlib.Path) -> pd.DataFrame | None:
    if f.is_symlink() or not f.is_file():
        return None  # ausente o symlink
    df = pd.read_csv(f)
    if df.empty or not set(_REQUIRED_COLS) <= set(df.columns):
        return None
    if df["unique_id"].astype(str).str.strip().eq("").any():
        return None  # unique_id vacío
    ds = pd.to_datetime(df["ds"], errors="coerce")
    if ds.isna().any():
        return None  # ds no parseable
    for col in ("y", "AutoBiTCN"):
        v = pd.to_numeric(df[col], errors="coerce")
        if v.isna().any() or not np.isfinite(v).all():
            return None  # no numérico/no finito
    keys = df[["unique_id", "ds", "y"]].copy()
    keys["ds"] = ds
    if keys[["unique_id", "ds"]].duplicated().any():
        return None  # (unique_id, ds) NO único dentro de la semilla
    return keys.sort_values(["unique_id", "ds"]).reset_index(drop=True)


def main() -> int:
    camp = pathlib.Path("reports/campaign")
    ref: pd.DataFrame | None = None
    for s in _SEEDS:
        keys = _seed_keys(camp / f"global_FAD_camp_auto_s{s}.csv")
        if keys is None:
            return 1  # semilla ausente/incompleta/incoherente
        if ref is None:
            ref = keys
        elif not keys.equals(ref):  # mismo número de filas Y mismas (unique_id, ds, y) ORDENADAS
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
