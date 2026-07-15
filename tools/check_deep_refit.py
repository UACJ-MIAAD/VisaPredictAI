#!/usr/bin/env python
"""¿La añada FAD camp_auto ya tiene filas BiTCN? (P0R.5 · R9.4/B66 — extraído del heredoc de
run_campaign_aq_tail.sh). Exit 0 si el CSV existe y contiene el modelo BiTCN; exit 1 si falta (⇒ el runbook
re-corre los 5 refits deep). Sin efectos secundarios."""

from __future__ import annotations

import pathlib
import sys

import pandas as pd


def main() -> int:
    f = pathlib.Path("reports/campaign/global_FAD_camp_auto_s1.csv")
    ok = f.exists() and "BiTCN" in set(pd.read_csv(f)["model"].unique())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
