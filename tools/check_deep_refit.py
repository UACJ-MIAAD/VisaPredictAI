#!/usr/bin/env python
"""¿Los 5 refits deep FAD camp_auto están COMPLETOS? (P0R.5 · R9.4/B66/B74 — extraído del heredoc de
run_campaign_aq_tail.sh). Exit 0 solo si las CINCO semillas s1…s5 existen, son no vacías, traen las columnas
`unique_id`/`ds`/`y`/`AutoBiTCN` (formato ancho de NeuralForecast), `AutoBiTCN` es totalmente finito y el
conjunto `(unique_id, ds, y)` es IDÉNTICO entre las cinco. Exit 1 ante cualquier ausencia/inconsistencia (⇒
el runbook re-corre los 5 refits). Sin efectos secundarios.

B74: el heredoc original solo miraba `s1` y buscaba una columna `model` que los CSV reales (anchos) no
tienen; no comprobaba el contrato real."""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

_SEEDS = (1, 2, 3, 4, 5)
_REQUIRED_COLS = {"unique_id", "ds", "y", "AutoBiTCN"}


def main() -> int:
    camp = pathlib.Path("reports/campaign")
    keysets: list[set] = []
    for s in _SEEDS:
        f = camp / f"global_FAD_camp_auto_s{s}.csv"
        if not f.is_file():
            return 1  # falta una semilla
        df = pd.read_csv(f)
        if df.empty or not _REQUIRED_COLS <= set(df.columns):
            return 1
        bitcn = pd.to_numeric(df["AutoBiTCN"], errors="coerce")
        if bitcn.isna().any() or not np.isfinite(bitcn).all():
            return 1  # AutoBiTCN incompleto/no-finito
        keysets.append(set(zip(df["unique_id"], df["ds"], df["y"], strict=True)))
    if any(ks != keysets[0] for ks in keysets):
        return 1  # (unique_id, ds, y) difieren entre semillas
    return 0


if __name__ == "__main__":
    sys.exit(main())
