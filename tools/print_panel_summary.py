#!/usr/bin/env python
"""Resumen de una línea del panel largo (filas · meses · F) — P0R.5 · R9.4/B66 (extraído del `-c` de
run_rederivation.sh). Solo lee `data/processed/visa_panel_long.csv`; sin efectos secundarios."""

from __future__ import annotations

import sys

import pandas as pd

from vp_data import config


def main() -> int:
    p = pd.read_csv(config.PANEL_PATH)
    n_f = int((p.status == "F").sum())
    print(f"panel: {len(p):,} filas · {p.bulletin_date.nunique()} meses · F={n_f:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
